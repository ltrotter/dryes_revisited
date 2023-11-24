from datetime import datetime
from typing import Callable, List
import itertools
import copy
import os

from ..variables.dryes_variable import DRYESVariable
from ..time_aggregation.time_aggregation import TimeAggregation

from ..lib.log import setup_logging, log
from ..lib.time import TimeRange, create_timesteps, ntimesteps_to_md
from ..lib.parse import substitute_values
from ..lib.io import check_data_range, save_dataarray_to_geotiff

class DRYESIndex:
    def __init__(self, input_variable: DRYESVariable,
                 timesteps_per_year: int,
                 options: dict,
                 output_paths: dict,
                 log_file: str = 'DRYES_log.txt') -> None:
        
        setup_logging(log_file)

        self.input_variable = input_variable
        self.timesteps_per_year = timesteps_per_year

        self.check_options(options)
        self.get_cases()

        self.output_paths = substitute_values(output_paths, output_paths, rec = False)
    
    def check_options(self, options: dict) -> dict:
        these_options = self.default_options.copy()
        these_options.update({'post_fn': None})
        for k in these_options.keys():
            if k in options:
                these_options[k] = options[k]
            else:
                log(f'No option {k} specified, using default value: {these_options[k]}.')

        # all of this is a way to divide the options into three bits:
        # - the time aggregation, which will affect the input data (self.time_aggregation)
        # - the options that will be used to calculate the parameters and the index (self.options)
        # - the post-processing function, which will be applied to the index (self.post_processing)
        options = self.make_time_aggregation(these_options)
        self.post_processing = options['post_fn']
        del options['post_fn']
        self.options = options

    def make_time_aggregation(self, options: dict) -> dict:
        # if the iscontinuous flag is not set, set it to False
        if not hasattr(self, 'iscontinuous'):
                self.iscontinuous = False

        # deal with the time aggregation options
        if not 'agg_fn' in options:
            self.time_aggregation = TimeAggregation(365)
            return options
        
        time_agg = TimeAggregation(self.timesteps_per_year)
        for agg_name, agg_fn in options['agg_fn'].items():
            if isinstance(agg_fn, Callable):
                time_agg.add_aggregation(agg_name, agg_fn)
            elif isinstance(agg_fn, tuple) or isinstance(agg_fn, list):
                if len(agg_fn) == 1:
                    time_agg.add_aggregation(agg_name, agg_fn[0])
                else:
                    time_agg.add_aggregation(agg_name, agg_fn[0], agg_fn[1])
                    # if we have a post-aggregation function, the calculation needs to be continuous
                    self.iscontinuous = True
            else:
                raise ValueError(f'Aggregation function {agg_name} not recognized.')

        self.time_aggregation = time_agg

        del options['agg_fn']

        return options

    def get_cases(self) -> None:

        # Similarly to the options, we have three layers of cases to deal with
        time_agg = list(self.time_aggregation.aggfun.keys()) # cases[0]
        options = self.options                               # cases[1]
        post_processing = self.post_processing               # cases[2]

        ## start with the time_aggregation
        agg_cases = []
        for i, agg_name in enumerate(time_agg):
            this_case = dict()
            this_case['id']   = i
            this_case['name'] = agg_name
            this_case['tags'] = {'options.agg_fn': agg_name}
            agg_cases.append(this_case)

        ## then add the options
        # get the options that need to be permutated and the ones that are fixed
        fixed_options = {k: v for k, v in options.items() if not isinstance(v, dict)}
        to_permutate = {k: list(v.keys()) for k, v in options.items() if isinstance(v, dict)}
        values_to_permutate = [v for v in to_permutate.values()]
        keys = list(to_permutate.keys())

        permutations = [dict(zip(keys, p)) for p in itertools.product(*values_to_permutate)]
        identifiers = copy.deepcopy(permutations)
        for permutation in permutations:
            permutation.update(fixed_options)

        cases_opts = []
        for permutation in permutations:
            this_case_opts = {}
            for k, v in permutation.items():
                # if this is one of the options that we permutated
                if isinstance(options[k], dict):
                    this_case_opts[k] = options[k][v]
                # if not, this is fixed
                else:
                    this_case_opts[k] = v
                    permutation[k] = ""
            cases_opts.append(this_case_opts)

        opt_cases = []
        for case, permutation, i in zip(cases_opts, permutations, range(len(identifiers))):
            this_case = dict()
            this_case['id']   = i
            this_case['name'] = ','.join(v for v in permutation.values() if v != "")
            this_case['tags'] = {'options.' + pk:pv for pk,pv in permutation.items()}
            this_case['options'] = case
            opt_cases.append(this_case)

        ## finally add the post-processing
        post_cases = []
        for post_name, post_fn in post_processing.items():
            this_case = dict()
            this_case['id']   = i
            this_case['name'] = post_name
            this_case['tags'] = {'options.post_fn': post_name}
            this_case['post_fn'] = post_fn
            post_cases.append(this_case)

        ## and combine them
        self.cases = {'agg': agg_cases, 'opt': opt_cases, 'post': post_cases}

    def compute(self, current: TimeRange, reference: TimeRange|Callable[[datetime], TimeRange]) -> None:
        
        # make the reference period a function of time, for extra flexibility
        if isinstance(reference, TimeRange):
            reference_fn = lambda time: reference
        elif isinstance(reference, Callable):
            reference_fn = reference

        # get the timesteps for which we need input data
        data_timesteps = self.make_data_timesteps(current, reference_fn)

        # get the data,
        # these will gather and compute the input data (checking if it is already available)
        # if we need a time aggregation, this will be done in the input variable
        input_data_path = self.make_input_data(data_timesteps)
        self.input_data_path = input_data_path

        # get the reference periods that we need to calculate parameters for
        reference_periods = self.make_reference_periods(current, reference_fn)

        # calculate the parameters
        for reference in reference_periods:
            self.make_parameters(reference)

        breakpoint()
        # calculate the index
        raw_index_path = self.make_index(current, reference_fn)
    
    def make_data_timesteps(self, current: TimeRange, reference_fn: Callable[[datetime], TimeRange]) -> List[datetime]:
        """
        This function will return the timesteps for which the data needs to be computed.
        """

        agg_timesteps_per_year = self.time_aggregation.timesteps_per_year

        # all of this will get the range for the data that is needed
        current_timesteps = create_timesteps(current.start, current.end, agg_timesteps_per_year)
        reference_start = set(reference_fn(time).start for time in current_timesteps)
        reference_end   = set(reference_fn(time).end   for time in current_timesteps)
        
        if self.iscontinuous:
            time_start = min(reference_start)
            time_end   = max(current_timesteps)
            return create_timesteps(time_start, time_end, agg_timesteps_per_year)
        else:
            reference_timesteps = create_timesteps(min(reference_start), max(reference_end), self.timesteps_per_year)
            return reference_timesteps + current_timesteps

    def make_reference_periods(self, current: TimeRange, reference_fn: Callable[[datetime], TimeRange]) -> List[TimeRange]:
        """
        This function will return the reference periods for which the parameters need to be computed.
        """

        # all of this will get the range for the data that is needed
        current_timesteps = create_timesteps(current.start, current.end, self.timesteps_per_year)
        references = set()
        for time in current_timesteps:
            this_reference = reference_fn(time)
            references.add((this_reference.start, this_reference.end))
        
        references = list(references)
        references.sort()

        references_as_tr = [TimeRange(start, end) for start, end in references]
        return references_as_tr
    
    def make_input_data(self, timesteps: List[datetime]) -> dict[str:str]:
        """
        This function will gather compute and aggregate the input data
        """
        variable = self.input_variable
        time_agg = self.time_aggregation
        
        # the time aggregator recognises only the 'agg_name' keyword
        # for the path name
        path = self.output_paths['data']
        path = substitute_values(path, {'var': variable.name, 'options.agg_fn': '{agg_name}'})

        # get the names of the aggregations to compute
        agg_cases = self.cases['agg']
        agg_names = [case['name'] for case in agg_cases]
        
        log(f'Making input data ({variable.name})...')
        # if there are no aggregations to compute, just get the data in the paths
        if len(agg_names) == 0:
            variable.path = path
            variable.make(TimeRange(min(timesteps), max(timesteps)))
            return path

        agg_paths = {agg_name: path.format(agg_name = agg_name) for agg_name in agg_names}

        time_range = TimeRange(min(timesteps), max(timesteps))
        available_timesteps = {name:list(check_data_range(path, time_range)) for name,path in agg_paths.items()}

        timesteps_to_compute = {}
        for agg_name in agg_names:
            # if there is no post aggregation function, we don't care for the order of the timesteps
            if agg_name not in time_agg.postaggfun.keys():
                timesteps_to_compute[agg_name] = [time for time in timesteps if time not in available_timesteps[agg_name]]
            # if there is a post aggregation function, we need to compute the timesteps in order
            else:
                timesteps_to_compute[agg_name] = []
                i = 0
                while(timesteps[i] not in available_timesteps[agg_name]) and i < len(timesteps):
                    timesteps_to_compute[agg_name].append(timesteps[i])
                    i += 1

        timesteps_to_iterate = set.union(*[set(timesteps_to_compute[agg_name]) for agg_name in agg_names])
        timesteps_to_iterate = list(timesteps_to_iterate)
        timesteps_to_iterate.sort()

        agg_data = {n:[] for n in agg_names}
        for i, time in enumerate(timesteps_to_iterate):
            log(f'Computing {time:%d-%m-%Y} ({i+1}/{len(timesteps_to_iterate)})...')
            for agg_name in agg_names:
                log(f'#Starting aggregation {agg_name}...')
                if time in timesteps_to_compute[agg_name]:
                    agg_data[agg_name].append(time_agg.aggfun[agg_name](variable, time))
        
        for agg_name in agg_names:
            log(f'#Completing time aggregation: {agg_name}...')
            if agg_name in time_agg.postaggfun.keys():
                agg_data[agg_name] = time_agg.postaggfun[agg_name](agg_data[agg_name], variable)

            n = 0
            for data in agg_data[agg_name]:
                this_time = timesteps_to_compute[agg_name][i]
                path_out = this_time.strftime(agg_paths[agg_name])
                save_dataarray_to_geotiff(data, path_out)
                n += 1
            
            log(f'#Saved {n} files to {os.path.dirname(self.output_paths['data'])}.')

        return agg_paths
    
    def make_parameters(self, history: TimeRange):
        log(f'Calculating parameters for {history.start:%d/%m/%Y}-{history.end:%d/%m/%Y}...')
        
        # get the output path template for the parameters
        output_path = self.output_paths['parameters']
        output_path = substitute_values(output_path, {'history_start': history.start, "history_end": history.end})
        
        # get the parameters that need to be calculated
        parameters = self.parameters

        # get the timesteps for which we need to calculate the parameters
        # this depends on the time aggregation step, not the index calculation step
        md_timesteps = ntimesteps_to_md(self.time_aggregation.timesteps_per_year)
        timesteps = [datetime(1900, month, day) for month, day in md_timesteps]
        time_range = TimeRange(min(timesteps), max(timesteps))
        # the year for time_range and timesteps is fictitious here, parameters don't have a year.

        history_years = list(range(history.start.year, history.end.year + 1))

        # parameters need to be calculated individually for each agg case
        agg_cases = self.cases['agg']
        for case in agg_cases:
            log(f' #{case["name"]}:')
            this_output_path = substitute_values(output_path, case['tags'])
            par_paths = {par: substitute_values(this_output_path, {'par': par}) for par in parameters}

            # check if anything has been calculated already
            done_timesteps = {par: check_data_range(par_path, time_range) for par, par_path in par_paths.items()}
            timesteps_to_do = {par: [time for time in timesteps if time not in done_timesteps[par]] for par in parameters}
            ndone = {par: len(timesteps) - len(timesteps_to_do[par]) for par in parameters}
            for par in parameters:
                log(f'  - {par}: {ndone[par]}/{len(timesteps)} timesteps already computed.')

            timesteps_to_iterate = set.union(*[set(timesteps_to_do[par]) for par in parameters])
            timesteps_to_iterate = list(timesteps_to_iterate)
            timesteps_to_iterate.sort()

            # if nothing needs to be calculated, skip
            if len(timesteps_to_iterate) == 0: continue

            log(f' #Iterating through {len(timesteps_to_iterate)} timesteps with missing thresholds.')
            for time in timesteps_to_iterate:
                month = time.month
                day   = time.day
                log(f'  - {day:02d}/{month:02d}')
                this_date = datetime(1900, month, day)
                par_to_calc = [par for par in parameters if this_date in timesteps_to_do[par]]

                all_dates  = [datetime(year, month, day) for year in history_years]
                data_dates = all_dates
                #data_dates = [date for date in all_dates if date >= history.start and date <= history.end]

                par_data = self.calc_parameters(data_dates, par_to_calc)
                for par in par_to_calc:
                    save_dataarray_to_geotiff(par_data[par], time.strftime(par_paths[par]))

    def make_index(self, current: TimeRange, reference_fn: Callable[[datetime], TimeRange]) -> str:
        log(f'Calculating index for {current.start:%d/%m/%Y}-{current.end:%d/%m/%Y}...')

        # get the timesteps for which we need to calculate the index
        timesteps = create_timesteps(current.start, current.end, self.timesteps_per_year)

        # check if anything has been calculated already

        for case in self.cases:
            case['tags']['post_process'] = ""
            this_index_path = substitute_values(self.output_paths['maps'], case['tags'])
            done_timesteps = check_data_range(this_index_path, current)
            timesteps_to_compute = [time for time in timesteps if time not in done_timesteps]
            if len(timesteps_to_compute) == 0:
                log(f' - case {case["name"]}: already calculated.')
                continue
                      
            log(f' - case {case["name"]}: {len(timesteps) - len(timesteps_to_compute)}/{len(timesteps)} timesteps already computed.')
            
            index = self.calc_index(timesteps, reference_fn)
            save_dataarray_to_geotiff(index, this_index_path)
            

        return self.calc_index(current, reference_fn)