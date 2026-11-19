import re


def normalize_nfe_access_key(value):
    if not isinstance(value, str):
        return ''
    return re.sub(r'[^0-9]', '', value)


def is_nfe_access_key(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{20}55[0-9]{22}', value) is not None
