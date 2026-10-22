"""Conservative, reproducible classification of explicit SENATRAN model names.

Only a registered vehicle maker plus an unambiguous propulsion marker is
accepted. Short codes such as EV and DM are restricted to makes that use them
for traction batteries; trailer and motorcycle model codes remain excluded.
"""
from src.matching import normalize

VERSION = 'explicit-name-1'

MAKERS = {
    'AUDI': 'Audi', 'BAIC': 'BAIC', 'BMW': 'BMW', 'BUSSCAR': 'Busscar',
    'BYD': 'BYD', 'CAOACHANGAN': 'Caoa Changan', 'CAOACHERY': 'Caoa Chery',
    'CHERY': 'Chery', 'CHEV': 'Chevrolet', 'CHEVROLET': 'Chevrolet',
    'DENZA': 'Denza', 'DONGFENG': 'Dongfeng', 'FANGCHENG': 'Fangchengbao',
    'FIAT': 'Fiat', 'FORD': 'Ford', 'FOTON': 'Foton', 'GEELY': 'Geely',
    'GMC': 'GMC', 'GWM': 'GWM', 'HYD': 'Hyundai', 'HYUNDAI': 'Hyundai',
    'INFINITI': 'Infiniti', 'JAC': 'JAC', 'JAECOO': 'Jaecoo',
    'JAG': 'Jaguar', 'JEEP': 'Jeep', 'JETOUR': 'Jetour', 'KIA': 'Kia',
    'LEXUS': 'Lexus', 'LR': 'Land Rover', 'MASERATI': 'Maserati',
    'M': 'Mercedes-Benz', 'MERCEDES': 'Mercedes-Benz',
    'MG': 'MG', 'MINI': 'Mini', 'MMC': 'Mitsubishi',
    'MPOLO': 'Marcopolo', 'NISSAN': 'Nissan', 'OMODA': 'Omoda',
    'PEUGEOT': 'Peugeot', 'PORSCHE': 'Porsche', 'RENAULT': 'Renault',
    'REVA': 'Reva', 'SAIC': 'MG', 'SANY': 'Sany', 'SOUEAST': 'Soueast',
    'SOUSA': 'Sousa', 'SUBARU': 'Subaru', 'TOYOTA': 'Toyota',
    'VOLVO': 'Volvo', 'VW': 'Volkswagen', 'YAMAHA': 'Yamaha',
    'YANGWANG': 'Yangwang',
}

# A two-letter model suffix can mean something unrelated to propulsion.
EV_MAKERS = frozenset({'BAIC', 'BMW', 'BYD', 'CAOACHANGAN', 'CHEV', 'CHEVROLET',
    'DENZA', 'DONGFENG', 'FOTON', 'GEELY', 'GMC', 'JAECOO', 'KIA', 'MG', 'NISSAN',
    'PEUGEOT', 'SAIC', 'SANY', 'TOYOTA'})
DM_MAKERS = frozenset({'BYD', 'CAOACHERY', 'CHERY', 'DENZA', 'FANGCHENG',
    'JETOUR', 'SOUEAST', 'YANGWANG'})

MODEL_ONLY = {'CIVIC HYBRID': ('Honda', 'Civic Hybrid', 'HEV')}


def explicit_powertrain(name):
    tokens = normalize(name).split()
    if not tokens:
        return None
    maker = tokens[0]
    if maker not in MAKERS:
        return None
    if maker == 'M' and tokens[1:2] != ['BENZ']:
        return None
    words = set(tokens[1:])
    if 'FUEL' in words and 'CELL' in words:
        return 'FCEV'
    if 'PHEV' in words or '4XE' in words or ('PLUG' in words and 'IN' in words):
        return 'PHEV'
    if maker == 'AUDI' and 'P' in words and 'HIBRIDO' in words:
        return 'PHEV'
    if 'DM' in words and maker in DM_MAKERS:
        return 'PHEV'
    if 'MHEV' in words:
        return 'MHEV'
    if 'HEV' in words:
        return 'HEV_FLEX' if 'FLEX' in words else 'HEV'
    if words.intersection({'HYBRID', 'HIBRIDO', 'HIBRIDA'}):
        return 'HYBRID'
    if words.intersection({'BEV', 'ELECTRIC', 'ELETRICO', 'ELETRICA'}):
        return 'BEV'
    if 'EV' in words and maker in EV_MAKERS:
        return 'BEV'
    return None


def classify(name):
    normalized = normalize(name)
    if normalized in MODEL_ONLY:
        maker, model, propulsion = MODEL_ONLY[normalized]
    else:
        propulsion = explicit_powertrain(normalized)
        if not propulsion:
            return None
        parts = normalized.split()
        maker = MAKERS[parts[0]]
        model = ' '.join(parts[1:])
        if parts[:2] == ['M', 'BENZ']:
            model = ' '.join(parts[2:])
        if not model:
            return None
    return {'manufacturer': maker, 'model': model, 'powertrain': propulsion,
            'category': 'unknown', 'vehicle_subtype': None, 'segment': None,
            'body_type': None, 'status': 'recognized',
            'evidence': 'SENATRAN: marca/modelo/versão com indicação explícita de propulsão; regra ' + VERSION}
