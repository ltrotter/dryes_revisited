from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import numpy as np

from typing import List

class TimeRange():
    def __init__(self, start: datetime, end: datetime):
        """
        Creates a TimeRange object. Useful to download data from a data source.
        If a timerange object is passed to data source, the data source will
        download all the data between the two dates.
        """
        self.isrange = True
        self.start = start
        self.end = end
    
    def __iter__(self):
        self.now = self.start
        return self
    
    def __next__(self):
        if self.now > self.end:
            raise StopIteration
        now = self.now
        self.now+= timedelta(days=1)
        return now

def create_timesteps(time_start: datetime, time_end: datetime, n_intervals: int) -> list[datetime]:
    """
    Creates a list of timesteps between two dates.
    n_intervals is the number of subdivisions of the year to consider.
    n_intervals can only take as value 1, 2, 3, 4, 6, 12, 24, 36, 365.

    """
    start_year = time_start.year
    end_year = time_end.year

    if n_intervals not in [1, 2, 3, 4, 6, 12, 24, 36, 365]:
        raise ValueError("Invalid number of intervals. Must be a positive integer that divides 12, 24, or 36 evenly.")

    if n_intervals == 1:
        timesteps =  [datetime(year,     1,   1) for year in range(start_year, end_year + 1)]
    elif n_intervals == 2:
        timesteps =  [datetime(year, month,   1) for year in range(start_year, end_year + 1) for month in [1, 7]]
    elif n_intervals == 3:
        timesteps =  [datetime(year, month,   1) for year in range(start_year, end_year + 1) for month in [1, 5, 9]]
    elif n_intervals == 4:
        timesteps =  [datetime(year, month,   1) for year in range(start_year, end_year + 1) for month in [1, 4, 7, 10]]
    elif n_intervals == 6:
        timesteps =  [datetime(year, month,   1) for year in range(start_year, end_year + 1) for month in [1, 3, 5, 7, 9, 11]]
    elif n_intervals == 12:
        timesteps =  [datetime(year, month,   1) for year in range(start_year, end_year + 1) for month in range(1, 13)]
    elif n_intervals == 24:
        timesteps =  [datetime(year, month, day) for year in range(start_year, end_year + 1) for month in range(1, 13) for day in [1, 16]]
    elif n_intervals == 36:
        timesteps =  [datetime(year, month, day) for year in range(start_year, end_year + 1) for month in range(1, 13) for day in [1, 11, 21]]
    elif n_intervals == 365:
        timesteps =  [time_start]
        new_timestep = time_start
        while new_timestep < time_end:
            new_timestep = new_timestep + timedelta(days=1)
            if (new_timestep.month, new_timestep.day) != (2, 29):
                timesteps.append(new_timestep)

    timesteps = [time for time in timesteps if time >= time_start and time <= time_end]
    return timesteps

def get_interval(date: datetime, num_intervals: int = 12) -> int:
    if num_intervals not in [1, 2, 3, 4, 6, 12, 24, 36]:
        raise ValueError("Invalid number of intervals. Must be a positive integer that divides 12, 24, or 36 evenly.")

    if num_intervals in [1, 2, 3, 4, 6, 12]:
        interval_length = 12 // num_intervals #this is in months
    elif num_intervals == 24:
        interval_length = 15 #this is in days
    else:
        interval_length = 10 #this is in days
    
    month = date.month
    day = date.day
    
    if num_intervals in [1, 2, 3, 4, 6, 12]:
        interval = np.ceil(month / interval_length)
    else:
        intervals_per_month = num_intervals // 12
        interval = (month - 1) * intervals_per_month + min(np.ceil(day / interval_length), intervals_per_month)
    
    return int(interval)

def get_interval_date(date: datetime, num_intervals: int = 12, end: bool = False) -> datetime:
    interval = get_interval(date, num_intervals)
    return(get_date_from_interval(interval, date.year, num_intervals, end))
    
def get_date_from_interval(interval: int, year: int, num_intervals: int = 12, end: bool = False) -> datetime:
    if num_intervals not in [1, 2, 3, 4, 6, 12, 24, 36]:
        raise ValueError("Invalid number of intervals. Must be a positive integer that divides 12, 24, or 36 evenly.")

    if num_intervals in [1, 2, 3, 4, 6, 12]:
        interval_length = 12 // num_intervals #this is in months
    elif num_intervals == 24:
        interval_length = 15 #this is in days
    else:
        interval_length = 10 #this is in days
    
    if num_intervals in [1, 2, 3, 4, 6, 12]:
        day = 1
        month = int((interval - 1) * interval_length + 1)
        if end:
            month += interval_length
            if month > 12:
                month = 1
                year += 1
        date = datetime(year, month, day)
        if end:
            date = date - timedelta(days=1)
        return date
    
    else:
        intervals_per_month = num_intervals // 12
        month = int(np.floor((interval - 1) / intervals_per_month) + 1)
        in_month_interval = (interval - 1) % intervals_per_month + 1
        day = (in_month_interval - 1) * interval_length + 1
        if end:
            if in_month_interval == intervals_per_month:
                month += 1
                day = 1
                if month > 12:
                    month = 1
                    year += 1
                date = datetime(year, month, day) - timedelta(days=1)
            else:
                day += interval_length - 1
                date = datetime(year, month, day)
        else:
            date = datetime(year, month, day)
        return date

def doy_to_md(doy:int) -> tuple[int, int]:
    """
    Converts day of year to month and day. Ignores 29 February.
    """
    
    date = datetime(1987, 1, 1) + timedelta(days = doy - 1)
    return (date.month, date.day)

def ntimesteps_to_md(timesteps_per_year: int) -> List[tuple[int, int]]:
    timesteps = create_timesteps(datetime(1987, 1, 1), datetime(1987, 12, 31), timesteps_per_year)
    return [(time.month, time.day) for time in timesteps] 

def get_window(time: datetime, size: int, unit: str) -> TimeRange:
        time_end = time - timedelta(days=1)
        if unit[-1] != 's': unit += 's'
        if unit in ['months', 'years', 'days', 'weeks']:
            time_start = time - relativedelta(**{unit: size})
        elif unit == 'dekads':
            if time_end == get_interval_date(time_end, 36, end = True):
                tmp_time = time_end
                for i in range(size): tmp_time = get_interval_date(tmp_time, 36) - timedelta(days=1)
                time_start = tmp_time + timedelta(days=1)
            else:
                time_start = time - timedelta(days = 10 * size)
        else:
            raise ValueError('Unit for average aggregator recognized: must be one of dekads, months, years, days, weeks')
        return TimeRange(time_start, time_end)