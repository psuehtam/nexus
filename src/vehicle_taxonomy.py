"""Canonical vehicle taxonomy shared by classification and publication."""
ELECTRIFIED_POWERTRAINS=frozenset({'BEV','PHEV','HEV','HEV_FLEX','HYBRID','MHEV','MHEV_12V','MHEV_48V','FCEV'})
HEAVY_CATEGORIES=frozenset({'bus','microbus','truck','tractor_truck','chassis_platform','heavy'})
CANONICAL_CATEGORIES=frozenset({'car','motorcycle','bus','microbus','truck','tractor_truck','van','utility','chassis_platform','other','unknown'})

def is_electrified(powertrain):return powertrain in ELECTRIFIED_POWERTRAINS
def is_heavy(category):return category in HEAVY_CATEGORIES
