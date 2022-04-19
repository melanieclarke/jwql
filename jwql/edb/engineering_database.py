#! /usr/bin/env python

"""Module for dealing with JWST DMS Engineering Database mnemonics.

This module provides ``jwql`` with convenience classes and functions
to retrieve and manipulate mnemonics from the JWST DMS EDB. It uses
the ``engdb_tools`` module of the ``jwst`` package to interface the
EDB directly.

Authors
-------

    - Johannes Sahlmann
    - Mees Fix
    - Bryan Hilbert

Use
---

    This module can be imported and used with

    ::

        from jwql.edb.engineering_database import get_mnemonic
        get_mnemonic(mnemonic_identifier, start_time, end_time)

    Required arguments:

    ``mnemonic_identifier`` - String representation of a mnemonic name.
    ``start_time`` - astropy.time.Time instance
    ``end_time`` - astropy.time.Time instance

Notes
-----
    There are two possibilities for MAST authentication:

    1. A valid MAST authentication token is present in the local
    ``jwql`` configuration file (config.json).
    2. The MAST_API_TOKEN environment variable is set to a valid
    MAST authentication token.

    When querying mnemonic values, the underlying MAST service returns
    data that include the datapoint preceding the requested start time
    and the datapoint that follows the requested end time.
"""
import calendar
from collections import OrderedDict
from datetime import datetime, timedelta
from numbers import Number
import os
import warnings

from astropy.io import ascii
from astropy.stats import sigma_clipped_stats
from astropy.table import Table
from astropy.time import Time
import astropy.units as u
from astroquery.mast import Mast
from bokeh.embed import components
from bokeh.models import BoxAnnotation, ColumnDataSource, DatetimeTickFormatter, HoverTool, Range1d
from bokeh.plotting import figure, output_file, show, save
import numpy as np

from jwst.lib.engdb_tools import ENGDB_Service
from jwql.utils.credentials import get_mast_base_url, get_mast_token

MAST_EDB_MNEMONIC_SERVICE = 'Mast.JwstEdb.Mnemonics'
MAST_EDB_DICTIONARY_SERVICE = 'Mast.JwstEdb.Dictionary'


class EdbMnemonic:
    """Class to hold and manipulate results of DMS EngDB queries."""
    def __add__(self, mnem):
        """Allow EdbMnemonic instances to be added (i.e. combine their data).
        info and metadata will not be touched. Data will be updated. Duplicate
        rows due to overlapping dates will be removed. The overlap is assumed to
        be limited to a single section of the end of once EdbMnemonic instance and
        the beginning of the other instance. Either one of the two instances to be
        added can contain the earlier dates. The function will check the starting
        date of each instance and treat the earlier starting date as the instance
        that is first. Blocks will be updated to account for removed duplicate rows.

        Parameters
        ----------
        mnem : jwql.edb.engineering_database.EdbMnemonic
            Instance to be added to the current instance

        Returns
        -------
        new_obj : jwql.edb.engineering_database.EdbMnemonic
            Summed instance
        """
        # Do not combine two instances of different mnemonics
        if self.mnemonic_identifier != mnem.mnemonic_identifier:
            raise ValueError((f'Unable to concatenate EdbMnemonic instances for {self.info["tlmMnemonic"]} '
                              'and {mnem.info["tlmMnemonic"]}.'))

        # Case where one instance has an empty data table
        if len(self.data["dates"]) == 0:
            return mnem
        if len(mnem.data["dates"]) == 0:
            return self

        if np.min(self.data["dates"]) < np.min(mnem.data["dates"]):
            early_dates = self.data["dates"].data
            late_dates = mnem.data["dates"].data
            early_data = self.data["euvalues"].data
            late_data = mnem.data["euvalues"].data
            early_blocks = self.blocks
            late_blocks = mnem.blocks
        else:
            early_dates = mnem.data["dates"].data
            late_dates = self.data["dates"].data
            early_data = mnem.data["euvalues"].data
            late_data = self.data["euvalues"].data
            early_blocks = mnem.blocks
            late_blocks = self.blocks

        # Remove any duplicates, based on the dates entries
        # Keep track of the indexes of the removed rows, so that any blocks
        # information can be updated
        all_dates = np.append(early_dates, late_dates)
        unique_dates, unq_idx = np.unique(all_dates, return_index=True)

        # Combine the data and keep only unique elements
        all_data = np.append(early_data, late_data)
        unique_data = all_data[unq_idx]

        # This assumes that if there is overlap between the two date arrays, that
        # the overlap all occurs in a single continuous block at the beginning of
        # the later set of dates. It will not do the right thing if you ask it to
        # (e.g.) interleave two sets of dates.
        overlap_len = len(unique_dates) - len(all_dates)

        # Shift the block values for the later instance to account for any removed
        # duplicate rows
        if late_blocks[0] is not None:
            new_late_blocks = late_blocks - overlap_len
            if early_blocks[0] is None:
                new_blocks = new_late_blocks
            else:
                new_blocks = np.append(early_blocks, new_late_blocks)
        else:
            if early_blocks[0] is not None:
                new_blocks = early_blocks
            else:
                new_blocks = [None]

        new_data = Table([unique_dates, unique_data], names=('dates', 'euvalues'))
        new_obj = EdbMnemonic(self.mnemonic_identifier, self.data_start_time, self.data_end_time,
                              new_data, self.meta, self.info, blocks=new_blocks)
        return new_obj

    def __init__(self, mnemonic_identifier, start_time, end_time, data, meta, info, blocks=[None]):
        """Populate attributes.

        Parameters
        ----------
        mnemonic_identifier : str
            Telemetry mnemonic identifier
        start_time : astropy.time.Time instance
            Start time
        end_time : astropy.time.Time instance
            End time
        data : astropy.table.Table
            Table representation of the returned data.
        meta : dict
            Additional information returned by the query
        info : dict
            Auxiliary information on the mnemonic (description,
            category, unit)
        blocks : list
            Index numbers corresponding to the beginning of separate blocks
            of data. This can be used to calculate separate statistics for
            each block.

        """

        self.mnemonic_identifier = mnemonic_identifier
        self.requested_start_time = start_time
        self.requested_end_time = end_time
        self.data = data

        self.mean = None
        self.median = None
        self.stdev = None
        self.median_times = None

        self.meta = meta
        self.info = info
        self.blocks = np.array(blocks)

        if len(self.data) == 0:
            self.data_start_time = None
            self.data_end_time = None
        else:
            self.data_start_time = np.min(self.data['dates'])
            self.data_end_time = np.max(self.data['dates'])
            if isinstance(self.data['euvalues'][0], Number) and 'TlmMnemonics' in self.meta:
                self.full_stats()

    def __len__(self):
        """Report the length of the data in the instance"""
        return len(self.data["dates"])

    def __mul__(self, mnem):
        """Allow EdbMnemonic instances to be multiplied (i.e. combine their data).
        info will be updated with new units if possible. Data will be updated.
        Blocks will not be updated, under the assumption that the times in self.data
        will all be kept, and therefore self.blocks will remain correct after
        multiplication.

        BLOCKS DO NEED TO BE UPDATED HERE, DUE TO POTENTIALLY LOSING EXTRAPOLATED ROWS!!!!

        Parameters
        ----------
        mnem : jwql.edb.engineering_database.EdbMnemonic
            Instance to be multiplied into the current instance

        Returns
        -------
        new_obj : jwql.edb.engineering_database.EdbMnemonic
            New object where the data table is the product of those in the inputs
        """
        # If the data has only a single entry, we won't be able to interpolate, and therefore
        # we can't multiply it. Return an empty EDBMnemonic instance
        if len(mnem.data["dates"].data) < 2:
            mnem.data["dates"] = []
            mnem.data["euvalues"] = []
            return mnem

        # First, interpolate the data in mnem onto the same times as self.data
        mnem.interpolate(self.data["dates"].data)

        # Extrapolation will not be done, so make sure that we account for any elements
        # that were removed rather than extrapolated. Find all the dates for which
        # data exists in both instances.
        common_dates, self_idx, mnem_idx = np.intersect1d(self.data["dates"], mnem.data["dates"],
                                                          return_indices=True)

        # We should be able to keep blocks from the shorter of the two arrays.
        # i.e. whichever array does not have elements removed in the intersection
        # command above
        if len(self_idx) == len(self.data):
            use_blocks = self.blocks
        elif len(mnem_idx) == len(mnem.data):
            use_blocks = mnem.blocks
        else:
            raise ValueError('Both EdbMnemonic instances changed lengths when searching for the intersection.')

        # Strip away any rows from the tables that are not common to both instances
        self_data = self.data[self_idx]
        mnem_data = mnem.data[mnem_idx]

        # Mulitply
        new_tab = Table()
        new_tab["dates"] = common_dates
        new_tab["euvalues"] = self_data["euvalues"] * mnem_data["euvalues"]

        new_obj = EdbMnemonic(self.mnemonic_identifier, self.requested_start_time, self.requested_end_time,
                              new_tab, self.meta, self.info, blocks=use_blocks)

        try:
            combined_unit = (u.Unit(self.info['unit']) * u.Unit(mnem.info['unit'])).compose()[0]
            new_obj.info['unit'] = f'{combined_unit}'
            new_obj.info['tlmMnemonic'] = f'{self.info["tlmMnemonic"]} * {mnem.info["tlmMnemonic"]}'
            new_obj.info['description'] = f'({self.info["description"]}) * ({mnem.info["description"]})'
        except KeyError:
            pass
        return new_obj

    def __str__(self):
        """Return string describing the instance."""
        return 'EdbMnemonic {} with {} records between {} and {}'.format(
            self.mnemonic_identifier, len(self.data), self.data_start_time,
            self.data_end_time)

    def block_stats(self, sigma=3):
        """Calculate stats for a mnemonic where we want a mean value for
        each block of good data, where blocks are separated by times where
        the data are ignored.

        Parameters
        ----------
        sigma : int
            Number of sigma to use for sigma clipping
        """
        means = []
        medians = []
        stdevs = []
        medtimes = []
        if type(self.data["euvalues"].data[0]) not in [np.str_, str]:
            for i, index in enumerate(self.blocks[0:-1]):
                if self.meta['TlmMnemonics'][0]['AllPoints'] != 0:
                    meanval, medianval, stdevval = sigma_clipped_stats(self.data["euvalues"].data[index:self.blocks[i + 1]], sigma=sigma)
                else:
                    meanval, medianval, stdevval = change_only_stats(self.data["dates"].data[index:self.blocks[i + 1]],
                                                                     self.data["euvalues"].data[index:self.blocks[i + 1]], sigma=sigma)
                medtimes.append(calc_median_time(self.data["dates"].data[index:self.blocks[i + 1]]))
                means.append(meanval)
                medians.append(medianval)
                stdevs.append(stdevval)
        else:
            # If the data are strings, then set the mean to be the data value at the block index
            for i, index in enumerate(self.blocks[0:-1]):
                meanval = self.data["euvalues"].data[index]
                medianval = meanval
                stdevval = 0
                medtimes.append(calc_median_time(self.data["dates"].data[index:self.blocks[i + 1]]))
                means.append(meanval)
                medians.append(medianval)
                stdevs.append(stdevval)
        self.mean = means
        self.median = medians
        self.stdev = stdevs
        self.median_times = medtimes

    def bokeh_plot(self, show_plot=False, savefig=False, out_dir='./', nominal_value=None, yellow_limits=None,
                   red_limits=None, title=None, xrange=(None, None), yrange=(None, None), return_components=True,
                   return_fig=False):
        """Make basic bokeh plot showing value as a function of time. Optionally add a line indicating
        nominal (expected) value, as well as yellow and red background regions to denote values that
        may be unexpected.

        Paramters
        ---------
        show_plot : bool
            If True, show plot on screen rather than returning div and script

        savefig : bool
            If True, file is saved to html file

        out_dir : str
            Directory into which the html file is saved

        nominal_value : float
            Expected or nominal value for the telemetry. If provided, a horizontal dashed line
            at this value will be added.

        yellow_limits : list
            2-element list giving the lower and upper limits outside of which the telemetry value
            is considered non-nominal. If provided, the area of the plot between these two values
            will be given a green background, and that outside of these limits will have a yellow
            background.

        red_limits : list
            2-element list giving the lower and upper limits outside of which the telemetry value
            is considered worse than in the yellow region. If provided, the area of the plot outside
            of these two values will have a red background.

        title : str
            Will be used as the plot title. If None, the mnemonic name and description (if present)
            will be used as the title

        xrange : tuple
            Tuple of min, max datetime values to use as the plot range in the x direction.

        yrange : tuple
            Tuple of min, max datetime values to use as the plot range in the y direction.

        return_components : bool
            If True, return the plot as div and script components

        return_fig : bool
            If True, return the plot as a bokeh Figure object

        Returns
        -------
        obj : list or bokeh.plotting.figure
            If return_components is True, return a list containing [div, script]
            If return_figre is True, return the bokeh figure itself
        """
        # Make sure that only one output type is specified, or bokeh will get mad
        options = np.array([show_plot, savefig, return_components, return_fig])
        if np.sum(options) > 1:
            trues = np.where(options)[0]
            raise ValueError((f'{options[trues]} are set to True in plot_every_change_data. Bokeh '
                              'will only allow one of these to be True.'))

        # If there are no data in the table, then produce an empty plot in the date
        # range specified by the requested start and end time
        if len(self.data["dates"]) == 0:
            null_dates = [self.requested_start_time, self.requested_end_time]
            null_vals = [0, 0]
            source = ColumnDataSource(data={'x': null_dates, 'y': null_vals})
        else:
            source = ColumnDataSource(data={'x': self.data['dates'], 'y': self.data['euvalues']})

        if savefig:
            filename = os.path.join(out_dir, f"telem_plot_{self.mnemonic_identifier.replace(' ','_')}.html")
            print(f'\n\nSAVING HTML FILE TO: {filename}')

        if self.info is None:
            units = 'Unknown'
        else:
            units = self.info["unit"]

        # Create a useful plot title if necessary
        if title is None:
            if 'description' in self.info:
                if len(self.info['description']) > 0:
                    title = f'{self.mnemonic_identifier} - {self.info["description"]}'
                else:
                    title = self.mnemonic_identifier
            else:
                title = self.mnemonic_identifier

        fig = figure(tools='pan,box_zoom,reset,wheel_zoom,save', x_axis_type='datetime',
                     title=title, x_axis_label='Time', y_axis_label=f'{units}')

        # For cases where the plot is empty or contains only a single point, force the
        # plot range to something reasonable
        if len(self.data["dates"]) < 2:
            fig.x_range = Range1d(self.requested_start_time - timedelta(days=1), self.requested_end_time)
            bottom, top = (-1, 1)
            if yellow_limits is not None:
                bottom, top = yellow_limits
            if red_limits is not None:
                bottom, top = red_limits
            fig.y_range = Range1d(bottom, top)

        data = fig.scatter(x='x', y='y', line_width=1, line_color='blue', source=source)

        if len(self.data["dates"]) == 0:
            data.visible = False
            if nominal_value is not None:
                fig.line(null_dates, np.repeat(nominal_value, len(null_dates)), color='black',
                         line_dash='dashed', alpha=0.5)
        else:
            # If there is a nominal value provided, plot a dashed line for it
            if nominal_value is not None:
                fig.line(self.data['dates'], np.repeat(nominal_value, len(self.data['dates'])), color='black',
                         line_dash='dashed', alpha=0.5)

        # If limits for warnings/errors are provided, create colored background boxes
        if yellow_limits is not None or red_limits is not None:
            fig = add_limit_boxes(fig, yellow=yellow_limits, red=red_limits)

        # Make the x axis tick labels look nice
        fig.xaxis.formatter = DatetimeTickFormatter(microseconds=["%d %b %H:%M:%S.%3N"],
                                                    seconds=["%d %b %H:%M:%S.%3N"],
                                                    hours=["%d %b %H:%M"],
                                                    days=["%d %b %H:%M"],
                                                    months=["%d %b %Y %H:%M"],
                                                    years=["%d %b %Y"]
                                                    )
        fig.xaxis.major_label_orientation = np.pi / 4

        hover_tool = HoverTool(tooltips=[('Value', '@y'),
                                         ('Date', '@x{%d %b %Y %H:%M:%S}')
                                        ], mode='mouse', renderers=[data])
        hover_tool.formatters={'@x': 'datetime'}

        fig.tools.append(hover_tool)

        # Force the axes' range if requested
        if xrange[0] is not None:
            fig.x_range.start = xrange[0].timestamp() * 1000.
        if xrange[1] is not None:
            fig.x_range.end = xrange[1].timestamp() * 1000.
        if yrange[0] is not None:
            fig.y_range.start = yrange[0]
        if yrange[1] is not None:
            fig.y_range.end = yrange[1]

        if savefig:
            output_file(filename=filename, title=self.mnemonic_identifier)
            save(fig)

        if show_plot:
            show(fig)
        if return_components:
            script, div = components(fig)
            return [div, script]
        if return_fig:
            return fig

    def change_only_add_points(self):
        """Tweak change-only data. Add an additional data point immediately prior to
        each original data point, with a value equal to that in the previous data point.
        This will help with filtering data based on conditions later, and will create a
        plot that looks more realistic, with only horizontal and vertical lines.
        """
        new_dates = [self.data["dates"][0]]
        new_vals = [self.data["euvalues"][0]]
        delta_t = timedelta(microseconds=1)
        for i, row in enumerate(self.data[1:]):
            new_dates.append(self.data["dates"][i] - delta_t)
            new_vals.append(self.data["euvalues"][i - 1])
        new_table = Table()
        new_table["dates"] = new_dates
        new_table["euvalues"] = new_vals
        self.data = new_table

    def daily_stats(self, sigma=3):
        """Calculate the statistics for each day in the data
        contained in data["data"]. Should we add a check for a
        case where the final block of time is <<1 day?

        Parameters
        ----------
        sigma : int
            Number of sigma to use for sigma clipping
        """
        min_date = np.min(self.data["dates"])
        date_range = np.max(self.data["dates"]) - min_date
        num_days = date_range.days
        num_seconds = date_range.seconds
        range_days = num_days + 1

        # Generate a list of times to use as boundaries for calculating means
        limits = np.array([min_date + timedelta(days=x) for x in range(range_days)])
        limits = np.append(limits, np.max(self.data["dates"]))

        means, meds, devs, times = [], [], [], []
        for i in range(len(limits) - 1):
            good = np.where((self.data["dates"] >= limits[i]) & (self.data["dates"] < limits[i + 1]))

            if self.meta['TlmMnemonics'][0]['AllPoints'] != 0:
                avg, med, dev = sigma_clipped_stats(self.data["euvalues"][good], sigma=sigma)
            else:
                avg, med, dev = change_only_stats(self.data["dates"][good], self.data["euvalues"][good], sigma=sigma)
            means.append(avg)
            meds.append(med)
            devs.append(dev)
            times.append(limits[i] + (limits[i + 1] - limits[i]) / 2.)
        self.mean = means
        self.median = meds
        self.stdev = devs
        self.median_times = times

    def full_stats(self, sigma=3):
        """Calculate the mean/median/stdev of the full compliment of data

        Parameters
        ----------
        sigma : int
            Number of sigma to use for sigma clipping
        """
        if self.meta['TlmMnemonics'][0]['AllPoints'] != 0:
            self.mean, self.median, self.stdev = sigma_clipped_stats(self.data["euvalues"], sigma=sigma)
        else:
            self.mean, self.median, self.stdev = change_only_stats(self.data["dates"], self.data["euvalues"], sigma=sigma)
        self.mean = [self.mean]
        self.median = [self.median]
        self.stdev = [self.stdev]
        self.median_times = [calc_median_time(self.data["dates"])]

    def interpolate(self, times):
        """Interpolate data euvalues at specified datetimes.

        Parameters
        ----------
        times : list
            List of datetime objects describing the times to interpolate to
        """
        new_tab = Table()

        # Change-only data is unique and needs its own way to be interpolated
        if self.meta['TlmMnemonics'][0]['AllPoints'] == 0:
            new_values = []
            new_dates = []
            for time in times:
                latest = np.where(self.data["dates"] <= time)[0]
                if len(latest) > 0:
                    new_values.append(self.data["euvalues"][latest[-1]])
                    new_dates.append(time)
            if len(new_values) > 0:
                new_tab["euvalues"] = np.array(new_values)
                new_tab["dates"] = np.array(new_dates)

        # This is for non change-only data
        else:
            # We can only linearly interpolate if we have more than one entry
            if len(self.data["dates"]) >= 2:
                interp_times = np.array([create_time_offset(ele, self.data["dates"][0]) for ele in times])
                mnem_times = np.array([create_time_offset(ele, self.data["dates"][0]) for ele in self.data["dates"]])

                # Do not extrapolate. Any requested interoplation times that are outside the range
                # or the original data will be ignored.
                good_times = ((interp_times >= mnem_times[0]) & (interp_times <= mnem_times[-1]))
                interp_times = interp_times[good_times]

                new_tab["euvalues"] = np.interp(interp_times, mnem_times, self.data["euvalues"])
                new_tab["dates"] = np.array([add_time_offset(ele, self.data["dates"][0]) for ele in interp_times])

            else:
                # If there are not enough data and we are unable to interpolate,
                # then set the data table to be empty
                new_tab["euvalues"] = np.array[()]
                new_tab["dates"] = np.array[()]

        # Adjust any block values to account for the interpolated data
        new_blocks = []
        if self.blocks is not None:
            for index in self.blocks[0:-1]:
                good = np.where(new_tab["dates"] >= self.data["dates"][index])[0]

                if len(good) > 0:
                    new_blocks.append(good[0])
            new_blocks.append(len(new_tab["dates"]))
            self.blocks = np.array(new_blocks)

        # Update the data in the instance.
        self.data = new_tab

    def plot_data_plus_devs(self, show_plot=False, savefig=False, out_dir='./', nominal_value=None, yellow_limits=None,
                            red_limits=None, xrange=(None, None), yrange=(None, None), title=None, return_components=True,
                            return_fig=False):
        """Make basic bokeh plot showing value as a function of time. Optionally add a line indicating
        nominal (expected) value, as well as yellow and red background regions to denote values that
        may be unexpected. Also add a plot of the mean value over time and in a second figure, a plot of
        the devaition from the mean.

        Paramters
        ---------
        show_plot : bool
            If True, show plot on screen rather than returning div and script

        savefig : bool
            If True, file is saved to html file

        out_dir : str
            Directory into which the html file is saved

        nominal_value : float
            Expected or nominal value for the telemetry. If provided, a horizontal dashed line
            at this value will be added.

        yellow_limits : list
            2-element list giving the lower and upper limits outside of which the telemetry value
            is considered non-nominal. If provided, the area of the plot between these two values
            will be given a green background, and that outside of these limits will have a yellow
            background.

        red_limits : list
            2-element list giving the lower and upper limits outside of which the telemetry value
            is considered worse than in the yellow region. If provided, the area of the plot outside
            of these two values will have a red background.

        xrange : tuple
            Tuple of min, max datetime values to use as the plot range in the x direction.

        yrange : tuple
            Tuple of min, max datetime values to use as the plot range in the y direction.

        title : str
            Will be used as the plot title. If None, the mnemonic name and description (if present)
            will be used as the title

        return_components : bool
            If True, return the plot as div and script components

        return_fig : bool
            If True, return the plot as a bokeh Figure object

        Returns
        -------
        obj : list or bokeh.plotting.figure
            If return_components is True, return a list containing [div, script]
            If return_figre is True, return the bokeh figure itself
        """
        # Make sure that only one output type is specified, or bokeh will get mad
        options = np.array([show_plot, savefig, return_components, return_fig])
        if np.sum(options) > 1:
            trues = np.where(options)[0]
            raise ValueError((f'{options[trues]} are set to True in plot_every_change_data. Bokeh '
                              'will only allow one of these to be True.'))

        # If there are no data in the table, then produce an empty plot in the date
        # range specified by the requested start and end time
        if len(self.data["dates"]) == 0:
            null_dates = [self.requested_start_time, self.requested_end_time]
            null_vals = [0, 0]
            data_dates = null_dates
            data_vals = null_vals
        else:
            data_dates = self.data['dates']
            data_vals = self.data['euvalues']
        source = ColumnDataSource(data={'x': data_dates, 'y': data_vals})

        if savefig:
            filename = os.path.join(out_dir, f"telem_plot_{self.mnemonic_identifier.replace(' ','_')}.html")
            print(f'\n\nSAVING HTML FILE TO: {filename}')

        if self.info is None:
            units = 'Unknown'
        else:
            units = self.info["unit"]

        # Create a useful plot title if necessary
        if title is None:
            if 'description' in self.info:
                if len(self.info['description']) > 0:
                    title = f'{self.mnemonic_identifier} - {self.info["description"]}'
                else:
                    title = self.mnemonic_identifier
            else:
                title = self.mnemonic_identifier

        fig = figure(tools='pan,box_zoom,reset,wheel_zoom,save', x_axis_type=None,
                     title=title, x_axis_label='Time',
                     y_axis_label=f'{units}')

        # For cases where the plot is empty or contains only a single point, force the
        # plot range to something reasonable
        if len(self.data["dates"]) < 2:
            fig.x_range = Range1d(self.requested_start_time - timedelta(days=1), self.requested_end_time)
            bottom, top = (-1, 1)
            if yellow_limits is not None:
                bottom, top = yellow_limits
            if red_limits is not None:
                bottom, top = red_limits
            fig.y_range = Range1d(bottom, top)

        data = fig.scatter(x='x', y='y', line_width=1, line_color='blue', source=source)

        # Plot the mean value over time
        if len(self.median_times) > 0:
            mean_data = fig.line(self.median_times, self.mean, line_width=1, line_color='orange', alpha=0.75)

        if len(self.data["dates"]) == 0:
            data.visible = False
            if nominal_value is not None:
                fig.line(null_dates, np.repeat(nominal_value, len(null_dates)), color='black',
                         line_dash='dashed', alpha=0.5)
        else:
            # If there is a nominal value provided, plot a dashed line for it
            if nominal_value is not None:
                fig.line(self.data['dates'], np.repeat(nominal_value, len(self.data['dates'])), color='black',
                         line_dash='dashed', alpha=0.5)

        # If limits for warnings/errors are provided, create colored background boxes
        if yellow_limits is not None or red_limits is not None:
            fig = add_limit_boxes(fig, yellow=yellow_limits, red=red_limits)

        hover_tool = HoverTool(tooltips=[('Value', '@y'),
                                         ('Date', '@x{%d %b %Y %H:%M:%S}')
                                         ], mode='mouse', renderers=[data])
        hover_tool.formatters = {'@x': 'datetime'}

        fig.tools.append(hover_tool)

        # Force the axes' range if requested
        if xrange[0] is not None:
            fig.x_range.start = xrange[0].timestamp() * 1000.
        if xrange[1] is not None:
            fig.x_range.end = xrange[1].timestamp() * 1000.
        if yrange[0] is not None:
            fig.y_range.start = yrange[0]
        if yrange[1] is not None:
            fig.y_range.end = yrange[1]

        # Now create a second plot showing the devitation from the mean
        fig_dev = figure(height=250, x_range=fig.x_range, tools="xpan,xwheel_zoom,xbox_zoom,reset", y_axis_location="right",
                         x_axis_type='datetime', x_axis_label='Time', y_axis_label=f'Data - Mean ({units})')

        # Interpolate the mean values so that we can subtract the original data
        interp_means = np.interp(data_dates, self.median_times, self.mean)

        # Calculate deviation from the mean
        dev = data_vals - interp_means

        # Plot
        fig_dev.line(data_dates, dev, color='red')

        # Make the x axis tick labels look nice
        fig_dev.xaxis.formatter = DatetimeTickFormatter(microseconds=["%d %b %H:%M:%S.%3N"],
                                                       seconds=["%d %b %H:%M:%S.%3N"],
                                                       hours=["%d %b %H:%M"],
                                                       days=["%d %b %H:%M"],
                                                       months=["%d %b %Y %H:%M"],
                                                       years=["%d %b %Y"]
                                                       )
        fig.xaxis.major_label_orientation = np.pi / 4

        # Place the two figures in a column object
        bothfigs = column(figa, fig_dev)

        if savefig:
            output_file(filename=filename, title=self.mnemonic_identifier)
            save(bothfigs)

        if show_plot:
            show(bothfigs)
        if return_components:
            script, div = components(bothfigs)
            return [div, script]
        if return_fig:
            return bothfigs

    def save_table(self, outname):
        """Save the EdbMnemonic instance

        Parameters
        ----------
        outname : str
            Name of text file to save information into
        """
        ascii.write(self.data, outname, overwrite=True)

    def timed_stats(self, duration, sigma=3):
        """Break up the data into chunks of the given duration. Calculate the
        mean value for each chunk.

        Parameters
        ----------
        duration : astropy.quantity.Quantity
            Length of time of each chunk of data

        sigma : int
            Number of sigma to use in sigma-clipping
        """
        duration_secs = duration.to('second').value
        date_arr = np.array(self.data["dates"])
        num_bins = (np.max(self.data["dates"]) - np.min(self.data["dates"])).total_seconds() / duration_secs

        # Round up to the next integer if there is a fractional number of bins
        num_bins = np.ceil(num_bins)

        self.mean = []
        self.median = []
        self.stdev = []
        self.median_times = []
        for i in range(int(num_bins)):
            min_date = self.data["dates"][0] + timedelta(seconds=i * duration_secs)
            max_date = min_date + timedelta(seconds=duration_secs)
            good = ((date_arr >= min_date) & (date_arr < max_date))
            if self.meta['TlmMnemonics'][0]['AllPoints'] != 0:
                avg, med, dev = sigma_clipped_stats(self.data["euvalues"][good], sigma=sigma)
            else:
                avg, med, dev = change_only_stats(self.data["dates"][good], self.data["euvalues"][good], sigma=sigma)
            self.mean.append(avg)
            self.median.append(med)
            self.stdev.append(dev)
            self.median_times.append(calc_median_time(self.data["dates"].data[good]))


def add_limit_boxes(fig, yellow=None, red=None):
    """Add green/yellow/red background colors

    Parameters
    ----------
    fig : bokeh.plotting.figure
        Bokeh figure of the telemetry values

    yellow : list
        2-element list of [low, high] values. If provided, the areas of the plot less than <low>
        and greater than <high> will be given a yellow background, to indicate an area
        of concern.

    red : list
        2-element list of [low, high] values. If provided, the areas of the plot less than <low>
        and greater than <high> will be given a red background, to indicate values that
        may indicate an error. It is assumed that the low value of red is less
        than the low value of yellow, and that the high value of red is
        greater than the high value of yellow.

    Returns
    -------
    fig : bokeh.plotting.figure
        Modified figure with BoxAnnotations added
    """
    if yellow is not None:
        green = BoxAnnotation(bottom=yellow[0], top=yellow[1], fill_color='chartreuse', fill_alpha=0.2)
        fig.add_layout(green)
        if red is not None:
            yellow_high = BoxAnnotation(bottom=yellow[1], top=red[1], fill_color='gold', fill_alpha=0.2)
            fig.add_layout(yellow_high)
            yellow_low = BoxAnnotation(bottom=red[0], top=yellow[0], fill_color='gold', fill_alpha=0.2)
            fig.add_layout(yellow_low)
            red_high = BoxAnnotation(bottom=red[1], top=red[1] + 100, fill_color='red', fill_alpha=0.1)
            fig.add_layout(red_high)
            red_low = BoxAnnotation(bottom=red[0] - 100, top=red[0], fill_color='red', fill_alpha=0.1)
            fig.add_layout(red_low)

        else:
            yellow_high = BoxAnnotation(bottom=yellow[1], top=yellow[1] + 100, fill_color='gold', fill_alpha=0.2)
            fig.add_layout(yellow_high)
            yellow_low = BoxAnnotation(bottom=yellow[0] - 100, top=yellow[0], fill_color='gold', fill_alpha=0.2)
            fig.add_layout(yellow_low)

    else:
        if red is not None:
            green = BoxAnnotation(bottom=red[0], top=red[1], fill_color='chartreuse', fill_alpha=0.2)
            fig.add_layout(green)
            red_high = BoxAnnotation(bottom=red[1], top=red[1] + 100, fill_color='red', fill_alpha=0.1)
            fig.add_layout(red_high)
            red_low = BoxAnnotation(bottom=red[0] - 100, top=red[0], fill_color='red', fill_alpha=0.1)
            fig.add_layout(red_low)

    return fig


def add_time_offset(offset, dt_obj):
    """Add an offset to an input datetime object

    Parameters
    ----------
    offset : float
        Number of seconds to be added

    dt_obj : datetime.datetime
        Datetime object to which the seconds are added

    Returns
    -------
    obj : datetime.datetime
        Sum of the input datetime objects and the offset seconds.
    """
    return dt_obj + timedelta(seconds=offset)


def calc_median_time(time_arr):
    """Calcualte the median time of the input time_arr

    Parameters
    ----------
    time_arr : numpy.ndarray
        1D array of datetime objects

    Returns
    -------
    med_time : datetime.datetime
        Median time, as a datetime object
    """
    med_time = time_arr[0] + ((time_arr[-1] - time_arr[0]) / 2.)
    return med_time


def change_only_bounding_points(date_list, value_list, starttime, endtime):
    """For data containing change-only values, where bracketing data outside
    the requested time span may be present, create data points at the starting
    and ending times. This can be helpful with later interpolations.

    Parameters
    ----------
    date_list : list
        List of datetime values

    value_list : list
        List of corresponding mnemonic values

    starttime : datetime.datetime
        Start time

    endtime : datetime.datetime
        End time

    Returns
    -------
    date_list : list
        List of datetime values

    value_list : list
        List of corresponding mnemonic values
    """
    date_list_arr = np.array(date_list)

    if isinstance(starttime, Time):
        starttime = starttime.datetime

    if isinstance(endtime, Time):
        endtime = endtime.datetime

    valid_idx = np.where((date_list_arr <= endtime) & (date_list_arr >= starttime))[0]
    before_startime = np.where(date_list_arr < starttime)[0]
    before_endtime = np.where(date_list_arr < endtime)[0]

    # The value at starttime is either the value of the last point before starttime,
    # or NaN if there are no points prior to starttime
    if len(before_startime) == 0:
        value0 = np.nan
    else:
        value0 = value_list[before_startime[-1]]

    # The value at endtime is NaN if there are no times before the endtime.
    # Otherwise the value is equal to the value at the last point before endtime
    if len(before_endtime) == 0:
        value_end = np.nan
    else:
        value_end = value_list[before_endtime[-1]]

    # Crop the arrays down to the times between starttime and endtime
    date_list = list(np.array(date_list)[valid_idx])
    value_list = list(np.array(value_list)[valid_idx])

    # Add an entry for starttime and another for endtime
    date_list.insert(0, starttime)
    value_list.insert(0, value0)
    date_list.append(endtime)
    value_list.append(value_end)

    return date_list, value_list


def change_only_stats(times, values, sigma=3):
    """Calculate the mean/median/stdev as well as the median time for a
    collection of change-only data.

    Parameters
    ----------
    times : list
        List of datetime objects

    values : list
        List of values corresponding to times

    sigma : float
        Number of sigma to use for sigma-clipping

    Returns
    -------
    meanval : float
        Mean of values

    medianval : float
        Median of values

    stdevval : float
        Standard deviation of values
    """
    # If there is only a single datapoint, then the mean will be
    # equal to it.
    if len(times) == 0:
        return None, None, None
    if len(times) == 1:
        return values, values, 0.
    else:
        times = np.array(times)
        values = np.array(values)
        delta_time = times[1:] - times[0:-1]

        time_fractions = delta_time / np.min(delta_time) * 100.
        arr_for_median = [[val] * int(time) for val, time in zip(values, time_fractions)]
        flat_list_for_median = [item for sublist in arr_for_median for item in sublist]
        meanval, medianval, stdevval = sigma_clipped_stats(flat_list_for_median, sigma=sigma)
    return meanval, medianval, stdevval


def create_time_offset(dt_obj, epoch):
    """Subtract input epoch from a datetime object and return the
    residual number of seconds

    Paramters
    ---------
    dt_obj : datetime.datetime
        Original datetiem object

    epoch : datetime.datetime
        Datetime to be subtracted from dt_obj

    Returns
    -------
    obj : float
        Number of seconds between dt_obj and epoch
    """
    if isinstance(dt_obj, Time):
        return (dt_obj - epoch).to(u.second).value
    elif isinstance(dt_obj, datetime):
        return (dt_obj - epoch).total_seconds()


def get_mnemonic(mnemonic_identifier, start_time, end_time):
    """Execute query and return a ``EdbMnemonic`` instance.

    The underlying MAST service returns data that include the
    datapoint preceding the requested start time and the datapoint
    that follows the requested end time.

    Parameters
    ----------
    mnemonic_identifier : str
        Telemetry mnemonic identifiers, e.g. ``SA_ZFGOUTFOV``

    start_time : astropy.time.Time or datetime.datetime
        Start time

    end_time : astropy.time.Time or datetime.datetime
        End time

    Returns
    -------
    mnemonic : instance of EdbMnemonic
        EdbMnemonic object containing query results
    """
    base_url = get_mast_base_url()
    service = ENGDB_Service(base_url)  # By default, will use the public MAST service.

    meta = service.get_meta(mnemonic_identifier)

    # If the mnemonic is stored as change-only data, then include bracketing values
    # outside of the requested start and stop times. These may be needed later to
    # translate change-only data into all-points data.
    if meta['TlmMnemonics'][0]['AllPoints'] == 0:
        bracket = True
    else:
        bracket = False

    data = service.get_values(mnemonic_identifier, start_time, end_time, include_obstime=True,
                              include_bracket_values=bracket)

    dates = [datetime.strptime(row.obstime.iso, "%Y-%m-%d %H:%M:%S.%f") for row in data]
    values = [row.value for row in data]

    if bracket:
        # For change-only data, check to see how many additional data points there are before
        # the requested start time and how many are after the requested end time. Note that
        # the max for this should be 1, but it's also possible to have zero (e.g. if you are
        # querying up through the present and there are no more recent data values.) Use these
        # to produce entries at the beginning and ending of the queried time range.
        dates, values = change_only_bounding_points(dates, values, start_time, end_time)

    data = Table({'dates': dates, 'euvalues': values})
    info = get_mnemonic_info(mnemonic_identifier)

    # Create and return instance
    mnemonic = EdbMnemonic(mnemonic_identifier, start_time, end_time, data, meta, info)

    # Convert change-only data to "regular" data. If this is not done, checking for
    # dependency conditions may not work well if there are a limited number of points.
    # Also, later interpolations won't be correct with change-only points since we are
    # doing linear interpolation.
    if bracket:
        if len(mnemonic) > 0:
            mnemonic.change_only_add_points()

    return mnemonic


def get_mnemonics(mnemonics, start_time, end_time):
    """Query DMS EDB with a list of mnemonics and a time interval.

    Parameters
    ----------
    mnemonics : list or numpy.ndarray
        Telemetry mnemonic identifiers, e.g. ``['SA_ZFGOUTFOV',
        'IMIR_HK_ICE_SEC_VOLT4']``
    start_time : astropy.time.Time instance
        Start time
    end_time : astropy.time.Time instance
        End time

    Returns
    -------
    mnemonic_dict : dict
        Dictionary. keys are the queried mnemonics, values are
        instances of EdbMnemonic
    """
    if not isinstance(mnemonics, (list, np.ndarray)):
        raise RuntimeError('Please provide a list/array of mnemonic_identifiers')

    mnemonic_dict = OrderedDict()
    for mnemonic_identifier in mnemonics:
        # fill in dictionary
        mnemonic_dict[mnemonic_identifier] = get_mnemonic(mnemonic_identifier, start_time, end_time)

    return mnemonic_dict


def get_mnemonic_info(mnemonic_identifier):
    """Return the mnemonic description.

    Parameters
    ----------
    mnemonic_identifier : str
        Telemetry mnemonic identifier, e.g. ``SA_ZFGOUTFOV``

    Returns
    -------
    info : dict
        Object that contains the returned data
    """
    mast_token = get_mast_token()
    return query_mnemonic_info(mnemonic_identifier, token=mast_token)


def is_valid_mnemonic(mnemonic_identifier):
    """Determine if the given string is a valid EDB mnemonic.

    Parameters
    ----------
    mnemonic_identifier : str
        The mnemonic_identifier string to be examined.

    Returns
    -------
    bool
        Is mnemonic_identifier a valid EDB mnemonic?
    """
    inventory = mnemonic_inventory()[0]
    if mnemonic_identifier in inventory['tlmMnemonic']:
        return True
    else:
        return False


def mnemonic_inventory():
    """Return all mnemonics in the DMS engineering database.
    No authentication is required, this information is public.
    Since this is a rather large and quasi-static table (~15000 rows),
    it is cached using functools.

    Returns
    -------
    data : astropy.table.Table
        Table representation of the mnemonic inventory.
    meta : dict
        Additional information returned by the query.
    """
    out = Mast.service_request_async(MAST_EDB_MNEMONIC_SERVICE, {})
    data, meta = process_mast_service_request_result(out)

    # convert numerical ID to str for homogenity (all columns are str)
    data['tlmIdentifier'] = data['tlmIdentifier'].astype(str)

    return data, meta


def process_mast_service_request_result(result, data_as_table=True):
    """Parse the result of a MAST EDB query.

    Parameters
    ----------
    result : list of requests.models.Response instances
        The object returned by a call to ``Mast.service_request_async``
    data_as_table : bool
        If ``True``, return data as astropy table, else return as json

    Returns
    -------
    data : astropy.table.Table
        Table representation of the returned data.
    meta : dict
        Additional information returned by the query
    """
    json_data = result[0].json()
    if json_data['status'] != 'COMPLETE':
        raise RuntimeError('Mnemonic query did not complete.\nquery status: {}\nmessage: {}'.format(
            json_data['status'], json_data['msg']))

    try:
        # timestamp-value pairs in the form of an astropy table
        if data_as_table:
            data = Table(json_data['data'])
        else:
            if len(json_data['data']) > 0:
                data = json_data['data'][0]
            else:
                warnings.warn('Query did not return any data. Returning None')
                return None, None
    except KeyError:
        warnings.warn('Query did not return any data. Returning None')
        return None, None

    # collect meta data
    meta = {}
    for key in json_data.keys():
        if key.lower() != 'data':
            meta[key] = json_data[key]

    return data, meta


def query_mnemonic_info(mnemonic_identifier, token=None):
    """Query the EDB to return the mnemonic description.

    Parameters
    ----------
    mnemonic_identifier : str
        Telemetry mnemonic identifier, e.g. ``SA_ZFGOUTFOV``
    token : str
        MAST token

    Returns
    -------
    info : dict
        Object that contains the returned data
    """
    parameters = {"mnemonic": "{}".format(mnemonic_identifier)}
    result = Mast.service_request_async(MAST_EDB_DICTIONARY_SERVICE, parameters)
    info = process_mast_service_request_result(result, data_as_table=False)[0]

    return info
