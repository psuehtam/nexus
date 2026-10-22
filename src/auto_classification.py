"""Conservative, reproducible classification of explicit SENATRAN model names.

Only a registered vehicle maker plus an unambiguous propulsion marker is
accepted. Short codes such as EV and DM are restricted to makes that use them
for traction batteries; trailer and motorcycle model codes remain excluded.
"""
from src.matching import normalize

VERSION = 'explicit-name-4'

# Category assertions are deliberately narrow. The monthly marca/modelo file
# has no vehicle-type column; these families have independent maker evidence.
HEAVY_MODELS = {
    ('FOTON', 'IBLUE EV 6T'): ('truck', 'https://fotondobrasil.com.br/wp-content/uploads/2024/06/Foton-iBlue-6T.pdf'),
    ('MPOLO', 'TORINO ELETRICO U'): ('bus', 'https://onibus.marcopolo.com.br/produtos/urbanos'),
    ('SANY', 'SYZ422 BEV'): ('truck', 'https://sanysouthafrica.com/sany-product-family-2022.pdf'),
}

PASSENGER_MAKERS = frozenset({'AUDI', 'BAIC', 'BMW', 'BYD', 'CAOACHANGAN',
    'CAOACHERY', 'CHERY', 'CHEV', 'CHEVROLET', 'DENZA', 'DONGFENG', 'FANGCHENG',
    'FIAT', 'FORD', 'GEELY', 'GMC', 'GWM', 'HONDA', 'HYD', 'HYUNDAI', 'INFINITI',
    'JAC', 'JAECOO', 'JAG', 'JEEP', 'JETOUR', 'KIA', 'LEXUS', 'LR', 'MASERATI',
    'M', 'MERCEDES', 'MG', 'MINI', 'MMC', 'NISSAN', 'OMODA', 'PEUGEOT',
    'PORSCHE', 'RENAULT', 'REVA', 'SAIC', 'SOUEAST', 'SUBARU', 'TESLA',
    'TOYOTA', 'VOLVO', 'VW', 'YANGWANG'})
PICKUP_MODELS = frozenset({('BYD', 'SHARK'), ('CHEV', 'SILVERADO EV RST'),
    ('FORD', 'F150 4X4 HYBRID'), ('GMC', 'SIERRA EV D MAX'),
    ('TOYOTA', 'TUNDRA HYBRID')})
VAN_MODELS = frozenset({('NISSAN', 'TOWNSTAR EV'), ('PEUGEOT', 'PARTNER RAPID EV')})

# The same short suffix occurs on combustion motorcycles and trailers. An
# explicit exclusion prevents these known non-candidates returning to review.
EXCLUDED_NAMES = frozenset({
    'GUZZI CALIFORNIA EV', 'MGUZZI BR CALIFORNIA EV',
    'BRAMONT EVADER EV 1000',
    'IVECO ETECH 440E42TZP', 'IVECO ETECH 740E42 TZ',
    'IVECO ETECH MP440E42TP', 'IVECO ETECH RODOCAR',
    'VW 17 190 PLG E TECH',
    'R DM REBOQUES BAU 1E', 'R DM REBOQUES FECHADA 1E',
    'R DM WORKSHOP RAILLY 2E', 'R ENGATESPARANA DM 3500',
    'R F PROPRIA DM',
})

# Government fuel tables contain both electrified and combustion records under
# this exact model code. The marca/modelo monthly file cannot separate them.
FUEL_AMBIGUOUS_NAMES = frozenset({'BUSSCAR URBPLUSS HIBRIDO'})


def category_for(name):
    tokens = normalize(name).split()
    if not tokens:
        return 'unknown', None
    maker, model = tokens[0], ' '.join(tokens[1:])
    if maker == 'VOLVO' and model.startswith('FM ELECTRIC '):
        return 'tractor_truck' if model.endswith('T') else 'truck', 'https://www.volvotrucks.com/en-en/trucks/electric/volvo-fm-electric.html'
    if (maker, model) in HEAVY_MODELS:
        return HEAVY_MODELS[(maker, model)]
    if maker == 'YAMAHA':
        return 'motorcycle', None
    if maker == 'SOUSA':
        return 'motorcycle', None
    if (maker, model) in PICKUP_MODELS or (maker == 'BYD' and model.startswith('SHARK ')) or (maker == 'GMC' and model.startswith('HUMMER EV ')):
        return 'utility', None
    if (maker, model) in VAN_MODELS or (maker == 'KIA' and model.startswith('CARNIVAL ')) or (maker == 'DENZA' and model.startswith('D9 ')) or (maker == 'VW' and model.startswith('ID BUZZ')):
        return 'van', None
    if maker in PASSENGER_MAKERS:
        return 'car', None
    return 'unknown', None


def pending_reason(name):
    normalized = normalize(name)
    if normalized in FUEL_AMBIGUOUS_NAMES:
        return ('O mesmo código aparece com combustíveis diferentes em tabelas oficiais; o arquivo mensal não distingue as unidades.',
                'SENATRAN — marca/modelo; SEFAZ/ES — tabela de ônibus por combustível')
    if normalized.startswith('BORAM '):
        return ('Há registros públicos deste código com combustíveis diferentes. O nome sozinho não identifica quais unidades são elétricas.',
                'SENATRAN — marca/modelo; tabela IPVA estadual com códigos de combustível divergentes')
    if normalized.startswith('JIANGSU DM '):
        return ('O mesmo código aparece como elétrico e a gasolina em tabela pública; o arquivo mensal não distingue as unidades.',
                'SENATRAN — marca/modelo; SEFAZ/SC — tabela IPVA por combustível')
    if normalized == 'LMABORGHINI HURACAN EV':
        return ('Nome não corresponde a um modelo elétrico confirmado por fonte do fabricante.',
                'SENATRAN — marca/modelo; fabricante e motorização não confirmados')
    return ('Nome sem indicação suficiente para classificação automática; excluído dos totais.',
            'SENATRAN — marca/modelo; nenhuma ação manual necessária')

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

MODEL_ONLY = {
    'CIVIC HYBRID': ('Honda', 'Civic Hybrid', 'HEV'),
    'RENAULT KOLEOS E TECH': ('Renault', 'Koleos E-Tech', 'HEV'),
}


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
    if normalized in FUEL_AMBIGUOUS_NAMES:
        return None
    if normalized in EXCLUDED_NAMES:
        return {'status': 'excluded', 'manufacturer': None, 'model': None,
                'powertrain': None, 'category': None, 'vehicle_subtype': None,
                'segment': None, 'body_type': None,
                'evidence': 'Código de modelo não comprova bateria de tração; regra ' + VERSION}
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
    category, category_source = category_for(maker + ' ' + model if normalized in MODEL_ONLY else normalized)
    return {'manufacturer': maker, 'model': model, 'powertrain': propulsion,
            'category': category, 'vehicle_subtype': None, 'segment': None,
            'body_type': None, 'status': 'recognized',
            'evidence': 'SENATRAN: marca/modelo/versão com indicação explícita de propulsão; regra ' + VERSION +
                        (('; tipo: ' + category_source) if category_source else '')}
