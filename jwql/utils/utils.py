"""Various utility functions for the jwql project.

Authors
-------

    Matthew Bourque
    Joe Hunkeler (filename_parser)
    Lauren Chambers

Use
---

    This module can be imported as such:

    >>> import utils
    settings = get_config()
"""

import json
import re
import os


def get_config():
    """Return a dictionary that holds the contents of the jwql config
    file.

    Returns
    -------
    settings : dict
        A dictionary that holds the contents of the config file.
    """

    with open('config.json', 'r') as config_file:
        settings = json.load(config_file)

    return settings

def filename_parser(filename):
    """Return a dictionary that contains the properties of a given
    JWST file (e.g. program ID, visit number, detector, etc.)

    Parameters
    ----------
    filename : str
        Path or name of JWST file to parse

    Returns
    -------
    dict
        Collection of file properties

    Raises
    ------
    ValueError
        When the provided file does not follow naming conventions
    """
    filename = os.path.basename(filename)

    elements = \
        re.compile(r"[a-z]+"
                   "(?P<program_id>\d{5})"
                   "(?P<observation>\d{3})"
                   "(?P<visit>\d{3})"
                   "_(?P<visit_group>\d{2})"
                   "(?P<parallel_seq_id>\d{1})"
                   "(?P<activity>\d{2})"
                   "_(?P<exposure_id>\d+)"
                   "_(?P<detector>\w+)"
                   "_(?P<suffix>\w+).*")

    jwst_file = elements.match(filename)

    if jwst_file is not None:
        filename_dict = jwst_file.groupdict()
    else:
        raise ValueError('Provided file {} does not follow JWST naming conventions (jw<PPPPP><OOO><VVV>_<GGSAA>_<EEEEE>_<detector >_<suffix> .fits)')

    return filename_dict
