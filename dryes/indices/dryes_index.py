from datetime import datetime
from typing import Callable, List, Iterable
import xarray as xr
from copy import deepcopy

from ..time_aggregation.time_aggregation import TimeAggregation
from ..io import IOHandler

from ..utils.log import setup_logging
from ..utils.time import TimeRange, create_timesteps, ntimesteps_to_md
from ..utils.parse import options_to_cases

class DRYESIndex:
    def __init__(self,
                 index_options: dict,
                 io_options: dict) -> None:
        
        # set the logging
        self.log = setup_logging(io_options['log'], io_options['log'].name)

        self._check_index_options(index_options)
        self._get_cases()

        self._check_io_options(io_options)
    
    def _check_index_options(self, options: dict) -> None:
        these_options = self.default_options.copy()
        these_options.update({'post_fn': None})
        for k in these_options.keys():
            if k in options:
                these_options[k] = options[k]
            else:
                self.log.info(f'No option {k} specified, using default value: {these_options[k]}.')

        # all of this is a way to divide the options into three bits:
        # - the time aggregation, which will affect the input data (self.time_aggregation)
        # - the options that will be used to calculate the parameters and the index (self.options)
        # - the post-processing function, which will be applied to the index (self.post_processing)
        options = self._make_time_aggregation(these_options)
        self.post_processing = options['post_fn']
        del options['post_fn']
        self.options = options

    def _make_time_aggregation(self, options: dict) -> dict:
        # if the iscontinuous flag is not set, set it to False
        if not hasattr(self, 'iscontinuous'):
                self.iscontinuous = False

        # deal with the time aggregation options
        if not 'agg_fn' in options:
            self.time_aggregation = TimeAggregation()
            return options
        
        time_agg = TimeAggregation()
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

    def _get_cases(self) -> None:

        # Similarly to the options, we have three layers of cases to deal with
        time_agg = list(self.time_aggregation.aggfun.keys()) # cases[0]
        options = self.options                               # cases[1]
        post_processing = self.post_processing               # cases[2]

        ## start with the time_aggregation
        agg_cases = []
        # if len(time_agg) == 0:
        #     agg_cases = [None]

        for i, agg_name in enumerate(time_agg):
            this_case = dict()
            this_case['id']   = i
            this_case['name'] = agg_name
            this_case['tags'] = {'agg_fn': agg_name}
            agg_cases.append(this_case)

        ## then add the options
        opt_cases = options_to_cases(options)

        ## finally add the post-processing, if it exists
        post_cases = []
        if post_processing is not None:
            for post_name, post_fn in post_processing.items():
                this_case = dict()
                this_case['id']   = i
                this_case['name'] = post_name
                this_case['tags'] = {'post_fn': post_name}
                this_case['post_fn'] = post_fn
                post_cases.append(this_case)

        ## and combine them
        self.cases = {'agg': agg_cases, 'opt': opt_cases, 'post': post_cases}

    def _check_io_options(self, io_options: dict) -> None:

        # check that we have all the necessary options
        # for most indices, we need 'data' (for aggregated input data) and 'data_raw' (for raw input data)
        # if we don't have 'data_raw', we check if the data needs to be aggregated, if not we can use 'data'
        has_agg = len(self.cases['agg']) > 0
        if has_agg:
            # if we have aggregations, we need both 'data' and 'data_raw'
            if 'data_raw' not in io_options or 'data' not in io_options:
                raise ValueError('Both data and data_raw must be specified.')
            # otherwise, we still need at least one of them
        elif 'data' not in io_options and 'data_raw' not in io_options:
            raise ValueError('Either data or data_raw must be specified.')
        elif 'data' not in io_options:
            io_options['data'] = io_options['data_raw']
        elif 'data_raw' not in io_options:
            io_options['data_raw'] = io_options['data']

        self._raw_data = io_options['data_raw']
        template = self._raw_data.get_template()

        self._data     = io_options['data']
        self._data.set_template(template)

        # check that we have output specifications for all the parameters in self.parameters
        self._parameters = {}
        for par in self.parameters:
            if par not in io_options:
                raise ValueError(f'No output path for parameter {par}.')
            self._parameters[par] = io_options[par]
            self._parameters[par].set_template(template)

        # check that we have an output specification for the index
        if 'index' not in io_options:
            raise ValueError('No output path for index.')
        self._index = io_options['index']
        self._index.set_template(template)

    def compute(self, current:   tuple[datetime, datetime],
                      reference: tuple[datetime, datetime]|Callable[[datetime], tuple[datetime, datetime]],
                      timesteps_per_year: int) -> None:
        
        # turn the current period into a TimeRange object
        current = TimeRange(current[0], current[1])
        raw_reference = deepcopy(reference)
        # make the reference period a function of time, for extra flexibility
        if isinstance(reference, tuple) or isinstance(reference, list):
            reference_fn = lambda time: TimeRange(raw_reference[0], raw_reference[1])
        elif isinstance(reference, Callable):
            reference_fn = lambda time: TimeRange(raw_reference(time)[0], raw_reference(time)[1])

        # get the timesteps for which we need input data
        if len(self.cases['agg']) == 0:
           agg_timesteps_per_year = 365
        else:
            agg_timesteps_per_year = timesteps_per_year
        data_timesteps = self.make_data_timesteps(current, reference_fn, agg_timesteps_per_year)

        # get the data, this will aggregate the data, if necessary
        self.make_input_data(data_timesteps)

        # get the timesteps for which we need to calculate the index
        timesteps = create_timesteps(current.start, current.end, timesteps_per_year)
        # get the reference periods that we need to calculate parameters for
        reference_periods = self.make_reference_periods(timesteps, reference_fn)

        # calculate the parameters
        for reference_ in reference_periods:
            self.make_parameters(reference_, timesteps_per_year)

        # calculate the index
        self.make_index(timesteps, reference_fn)
    
    def make_data_timesteps(self, current: TimeRange,
                            reference_fn: Callable[[datetime], TimeRange],
                            timesteps_per_year: int) -> List[datetime]:
        """
        This function will return the timesteps for which the data needs to be computed.
        """

        # all of this will get the range for the data that is needed
        current_timesteps = create_timesteps(current.start, current.end, timesteps_per_year)
        reference_start = set(reference_fn(time).start for time in current_timesteps)
        reference_end   = set(reference_fn(time).end   for time in current_timesteps)
        if self.iscontinuous:
            time_start = min(reference_start)
            time_end   = max(current_timesteps)
            return create_timesteps(time_start, time_end, timesteps_per_year)
        else:
            reference_timesteps = create_timesteps(min(reference_start), max(reference_end), timesteps_per_year)
            all_timesteps = set.union(set(reference_timesteps), set(current_timesteps))
            all_timesteps = list(all_timesteps)
            all_timesteps.sort()
            return all_timesteps

    def make_reference_periods(self, current_timesteps: Iterable[datetime],
                               reference_fn: Callable[[datetime], TimeRange]) -> List[TimeRange]:
        """
        This function will return the reference periods for which the parameters need to be computed.
        """

        references = set()
        for time in current_timesteps:
            this_reference = reference_fn(time)
            references.add((this_reference.start, this_reference.end))
        
        references = list(references)
        references.sort()

        references_as_tr = [TimeRange(start, end) for start, end in references]
        return references_as_tr
    
    def make_input_data(self, timesteps: List[datetime]):# -> dict[str:str]:
        """
        This function will gather compute and aggregate the input data
        """

        variable_in = self._raw_data
        variable_out = self._data
        time_agg = self.time_aggregation
        
        # if there are no aggregations to compute, just get the data in the paths
        agg_cases = self.cases['agg']
        if len(agg_cases) == 0:
            return

        # get the names of the aggregations to compute
        agg_names = [case['name'] for case in agg_cases]

        # check what timesteps have already been computed for each aggregation
        timesteps_to_compute = {agg_name:[] for agg_name in agg_names}
        time_range = TimeRange(min(timesteps), max(timesteps))
        for agg_name in agg_names:
            available_ts = variable_out.get_times(time_range, agg_fn = agg_name)

            # if there is no post aggregation function, we don't care for the order of the timesteps
            # and can just compute the missing ones
            if agg_name not in time_agg.postaggfun.keys():
                these_ts_to_compute = [time for time in timesteps if time not in available_ts]
            # if there is a post aggregation function, each timesteps depends on the previous one(s)
            # so we need to compute them in order TODO: check if this works as expected
            else:
                these_ts_to_compute = []
                i = 0
                while(timesteps[i] not in available_ts) and i < len(timesteps):
                    these_ts_to_compute.append(timesteps[i])
                    i += 1
            timesteps_to_compute[agg_name] = these_ts_to_compute

        timesteps_to_iterate = set.union(*[set(timesteps_to_compute[agg_name]) for agg_name in agg_names])
        timesteps_to_iterate = list(timesteps_to_iterate)
        timesteps_to_iterate.sort()

        if len(timesteps_to_iterate) > 0:
            self.log.info(f'Aggregating input data ({variable_in.name})...')

        agg_data = {n:[] for n in agg_names if agg_name in time_agg.postaggfun.keys()}
        for i, time in enumerate(timesteps_to_iterate):
            self.log.info(f' #Timestep {time:%d-%m-%Y} ({i+1}/{len(timesteps_to_iterate)})...')
            for agg_name in agg_names:
                self.log.info(f'  Aggregation {agg_name}...')
                if time in timesteps_to_compute[agg_name]:
                    data = time_agg.aggfun[agg_name](variable_in, time)
                    if agg_name not in time_agg.postaggfun.keys():
                        variable_out.write_data(data, time = time, agg_fn = agg_name)
                    else:
                        agg_data[agg_name].append(data)
        
        for agg_name in agg_names:
            if agg_name in time_agg.postaggfun.keys():
                self.log.info(f'Completing time aggregation: {agg_name}...')
                agg_data[agg_name] = time_agg.postaggfun[agg_name](agg_data[agg_name], variable_in)

                for i, data in enumerate(agg_data[agg_name]):
                    this_time = timesteps_to_compute[agg_name][i]
                    variable_out.write_data(data, time = this_time, agg_fn = agg_name)
    
    def make_parameters(self,
                        history: TimeRange,
                        timesteps_per_year: int) -> None:
        self.log.info(f'Calculating parameters for {history.start:%d/%m/%Y}-{history.end:%d/%m/%Y}...')

        # get the parameters that need to be calculated
        parameters = self.parameters
        # get the output path template for the parameters
        for par in self.parameters:
            self._parameters[par].update(history_start = history.start, history_end = history.end, in_place = True)

        # get the timesteps for which we need to calculate the parameters
        # this depends on the time aggregation step, not the index calculation step
        md_timesteps = ntimesteps_to_md(timesteps_per_year)
        timesteps = [datetime(1900, month, day) for month, day in md_timesteps]
        # the year for time_range and timesteps is fictitious here, parameters don't have a year.

        # parameters need to be calculated individually for each agg case
        agg_cases = self.cases['agg']
        if len(agg_cases) == 0: agg_cases = [None]
        for agg in agg_cases:
            if agg is not None:
                self.log.info(f' #Aggregation {agg["name"]}:')
                agg_tags = agg['tags']
                #par_paths = {par:substitute_values(path, agg['tags']) for par, path in output_paths.items()}
                #in_path = self.input_data_path[agg["name"]]
            else:
                agg_tags = {}
                #par_paths = output_paths
                #in_path = self.input_data_path

            variable   = self._data.update(**agg_tags)
            parameters = {p: self._parameters[p].update(**agg_tags) for p in self.parameters}
            self.make_parameter_1agg(variable, parameters, history, timesteps)

    def make_parameter_1agg(self,
                            variable: IOHandler,
                            parameters: dict[str:IOHandler],
                            history: TimeRange,
                            timesteps: List[datetime]) -> dict:
            
            # check timesteps that have already been calculated for each parameter
            timesteps_to_do = {}
            for parname, par in parameters.items():
                for case in self.cases['opt']:
                    this_ts_done = par.get_times(TimeRange(min(timesteps), max(timesteps)), **case['tags'])
                    this_ts_todo = [time for time in timesteps if time not in this_ts_done]
                    for ts in this_ts_todo:
                        if ts not in timesteps_to_do:
                            timesteps_to_do[ts] = {}
                        if parname not in timesteps_to_do[ts]:
                            timesteps_to_do[ts][parname] = []
                        timesteps_to_do[ts][parname].append(case['id'])

            # if nothing needs to be calculated, skip
            if len(timesteps_to_do) == 0: return
            self.log.info(f'  -Iterating through {len(timesteps_to_do)} timesteps with missing parameters.')
            for time, par_cases in timesteps_to_do.items():
                month = time.month
                day   = time.day
                self.log.info(f'   {day:02d}/{month:02d}')

                pars_data = self.calc_parameters(time, variable, history, par_cases)

                for parname in pars_data:
                    par = parameters[parname]
                    for case, data in pars_data[parname].items():
                        tags = self.cases['opt'][case]['tags']
                        par.write_data(data, time = time, time_format = '%d/%m', **tags)

    def make_index(self, timesteps: List[datetime], reference_fn: Callable[[datetime], TimeRange]) -> str:
        self.log.info(f'Calculating index for {min(timesteps):%d/%m/%Y}-{max(timesteps):%d/%m/%Y}...')

        # check if anything has been calculated already
        agg_cases = self.cases['agg'] if len(self.cases['agg']) > 0 else [{}]
        for agg in agg_cases:
            agg_tags = agg['tags'] if 'tags' in agg else {}
            agg_name = agg['name'] if 'name' in agg else ''

            for case_ in self.cases['opt']:
                case = case_.copy()
                case['tags'].update(agg_tags)
                case['tags']['post_fn'] = ""
                index = self._index.update(**case['tags'])

                case['name'] = case['name'] if len(agg_name) == 0 else ', '.join([f'Aggregation {agg_name}', case['name']])

                ts_done = index.get_times(TimeRange(min(timesteps), max(timesteps)))
                ts_todo = [time for time in timesteps if time not in ts_done]

                if len(ts_todo) == 0:
                    self.log.info(f' #Case {case["name"]}: already calculated.')
                else:        
                    self.log.info(f' #Case {case["name"]}: {len(timesteps) - len(ts_todo)}/{len(timesteps)} timesteps already computed.')
                    for time in ts_todo:
                        self.log.info(f'   {time:%d/%m/%Y}')
                        history = reference_fn(time)
                        case['tags'].update({'history_start': history.start, 'history_end': history.end})
                        index_data = self.calc_index(time, history, case)
                        index.write_data(index_data, time = time)

                # now do the post-processing
                for post_case in self.cases['post']:
                    case['tags'].update(post_case['tags'])
                    ppindex = self._index.update(**case['tags'])
                    ts_done = ppindex.get_times(TimeRange(min(timesteps), max(timesteps)))
                    ts_todo = [time for time in timesteps if time not in ts_done]
                    if len(ts_todo) == 0:
                        self.log.info(f'  Post-processing {post_case["name"]}: already calculated.')
                        continue
                    self.log.info(f'  Post-processing {post_case["name"]}: {len(timesteps) - len(ts_todo)}/{len(timesteps)} timesteps already computed.')
                    for time in ts_todo:
                        self.log.info(f'   {time:%d/%m/%Y}')
                        history = reference_fn(time)
                        case['tags'].update({'history_start': history.start, 'history_end': history.end})
                        index_data = index.get_data(time)
                        post_fn = post_case['post_fn']
                        ppindex_data = post_fn(index_data)
                        ppindex.write_data(ppindex_data, time = time)

    def calc_parameters(self,
                        time: datetime,
                        variable: IOHandler,
                        history: TimeRange,
                        par_and_cases: dict[str:List[int]]) -> dict[str:dict[int:xr.DataArray]]:
        """
        time, variable, history, par_cases
        Calculates the parameters for the index.
        par_and_cases is a dictionary with the following structure:
        {par: [case1, case2, ...]}
        indicaing which cases from self.cases['opt'] need to be calculated for each parameter.

        The output is a dictionary with the following structure:
        {par: {case1: parcase1, case2: parcase1, ...}}
        where parcase1 is the parameter par for case1 as a xarray.DataArray.
        """
        raise NotImplementedError
    
    def calc_index(self, time,  reference: TimeRange, case: dict) -> xr.DataArray:
        """
        Calculates the index for the given time and reference period.
        """
        raise NotImplementedError