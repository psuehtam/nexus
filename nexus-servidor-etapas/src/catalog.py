"""Versioned, reviewed rules. Adding a family expands coverage of traction-battery vehicles."""
from functools import lru_cache
from sqlalchemy import select, insert
from src.db import engine, models, batteries, sources
from src.connectors.public import now, CATALOG

PBE = 'https://www.gov.br/inmetro/pt-br/assuntos/regulamentacao/avaliacao-da-conformidade/programa-brasileiro-de-etiquetagem/tabelas-de-eficiencia-energetica/veiculos-automotivos-pbe-veicular/mascara-pbev-2026_19_jan-rev01.pdf/@@download/file'
OEM = 'https://www.byd.com/material/__CN/byd-site/br/fichas-tecnicas-2026/update-13-07-2026/07-13-2026---ficha-txiunica/BYD_DolphinGS_V2.pdf'
TOYOTA = 'https://www.toyota.com.br/'
HONDA = 'https://www.honda.com.br/'
GWM = 'https://www.gwmmotors.com.br/'
TESLA = 'https://www.tesla.com/pt_br'
NISSAN = 'https://www.nissan.com.br/'
RENAULT = 'https://www.renault.com.br/'
VOLVO = 'https://www.volvocars.com/br/'
FIAT = 'https://www.fiat.com.br/'

# Longest prefixes must win (Seal DM before Seal; Dolphin Mini before Dolphin).
# propulsion: BEV, PHEV, HEV, HEV_FLEX, FCEV.
FAMILIES = [
    ('Mercedes-Benz', 'eO500U', ['M BENZ EO500U', 'MERCEDES BENZ EO500U'], 'BEV'),
    ('BYD', 'Dolphin Mini', ['BYD DOLPHIN MINI'], 'BEV'),
    ('BYD', 'Dolphin', ['BYD DOLPHIN'], 'BEV'),
    ('BYD', 'Seal DM-i', ['BYD SEAL DM I', 'BYD SEAL DM', 'BYD SEAL U DM'], 'PHEV'),
    ('BYD', 'Seal', ['BYD SEAL'], 'BEV'),
    ('BYD', 'Yuan Plus', ['BYD YUAN PLUS'], 'BEV'),
    ('BYD', 'Yuan Pro', ['BYD YUAN PRO'], 'BEV'),
    ('BYD', 'Song Plus', ['BYD SONG PLUS', 'BYD SONG PLUS DM', 'BYD SONG PLUS DM I'], 'PHEV'),
    ('BYD', 'Song Pro', ['BYD SONG PRO', 'BYD SONG PRO DM'], 'PHEV'),
    ('BYD', 'King', ['BYD KING', 'BYD KING DM', 'BYD KING DM I'], 'PHEV'),
    ('BYD', 'Han', ['BYD HAN'], 'BEV'),
    ('BYD', 'Tang', ['BYD TANG'], 'PHEV'),
    ('BYD', 'Sealion 7', ['BYD SEALION 7', 'BYD SEALION07'], 'BEV'),
    ('GWM', 'Ora 03', ['GWM ORA 03', 'GWM ORA03'], 'BEV'),
    ('GWM', 'Ora 07', ['GWM ORA 07', 'GWM ORA07'], 'BEV'),
    ('GWM', 'Haval H6 PHEV', ['GWM HAVAL H6 PHEV', 'GWM HAVAL H6 GT PHEV', 'HAVAL H6 PHEV'], 'PHEV'),
    ('GWM', 'Haval H6 HEV', ['GWM HAVAL H6 HEV', 'HAVAL H6 HEV'], 'HEV'),
    ('Volvo', 'EX30', ['VOLVO EX30'], 'BEV'),
    ('Volvo', 'EX90', ['VOLVO EX90'], 'BEV'),
    ('Volvo', 'XC40 Recharge', ['VOLVO XC40 RECHARGE', 'VOLVO XC40 BEV'], 'BEV'),
    ('Renault', 'Kwid E-Tech', ['RENAULT E KWID', 'RENAULT KWID E TECH', 'RENAULT KWID ETECH'], 'BEV'),
    ('Renault', 'Zoe', ['RENAULT ZOE'], 'BEV'),
    ('Renault', 'Megane E-Tech', ['RENAULT MEGANE E TECH', 'RENAULT MEGANE ETECH'], 'BEV'),
    ('JAC', 'E-JS1', ['JAC E JS1', 'JAC EJS1'], 'BEV'),
    ('JAC', 'E-JS4', ['JAC E JS4', 'JAC EJS4'], 'BEV'),
    ('Fiat', '500e', ['FIAT 500E'], 'BEV'),
    ('Peugeot', 'E-208', ['PEUGEOT E 208', 'PEUGEOT E208'], 'BEV'),
    ('Nissan', 'Leaf', ['NISSAN LEAF'], 'BEV'),
    ('Tesla', 'Model 3', ['TESLA MODEL 3', 'TESLA M3'], 'BEV'),
    ('Tesla', 'Model Y', ['TESLA MODEL Y'], 'BEV'),
    ('Tesla', 'Model S', ['TESLA MODEL S'], 'BEV'),
    ('Tesla', 'Model X', ['TESLA MODEL X'], 'BEV'),
    ('Chevrolet', 'Bolt', ['CHEVROLET BOLT', 'GM BOLT'], 'BEV'),
    ('Caoa Chery', 'iCar', ['CAOA CHERY ICAR', 'CHERY ICAR', 'CAOACHERRY ICAR'], 'BEV'),
    ('Caoa Chery', 'Arrizo 6e', ['CAOA CHERY ARRIZO 6E', 'CHERY ARRIZO 6E'], 'BEV'),
    ('Caoa Chery', 'Tiggo 7 PHEV', ['CAOA CHERY TIGGO 7 PHEV', 'CHERY TIGGO 7 PHEV', 'TIGGO 7 PHEV'], 'PHEV'),
    ('Caoa Chery', 'Tiggo 8 PHEV', ['CAOA CHERY TIGGO 8 PHEV', 'CHERY TIGGO 8 PHEV', 'TIGGO 8 PHEV'], 'PHEV'),
    ('BMW', 'iX1', ['BMW IX1'], 'BEV'),
    ('BMW', 'iX3', ['BMW IX3'], 'BEV'),
    ('BMW', 'i4', ['BMW I4'], 'BEV'),
    ('BMW', 'iX', ['BMW IX'], 'BEV'),
    ('Mini', 'Cooper SE', ['MINI COOPER SE', 'MINI COOPER E'], 'BEV'),
    ('Porsche', 'Taycan', ['PORSCHE TAYCAN'], 'BEV'),
    ('Audi', 'Q8 e-tron', ['AUDI Q8 E TRON', 'AUDI E TRON'], 'BEV'),
    ('Mercedes-Benz', 'EQA', ['MERCEDES BENZ EQA', 'MERCEDES EQA'], 'BEV'),
    ('Mercedes-Benz', 'EQB', ['MERCEDES BENZ EQB', 'MERCEDES EQB'], 'BEV'),
    ('Mercedes-Benz', 'EQC', ['MERCEDES BENZ EQC', 'MERCEDES EQC'], 'BEV'),
    ('Mercedes-Benz', 'EQS', ['MERCEDES BENZ EQS', 'MERCEDES EQS'], 'BEV'),
    ('Kia', 'Niro EV', ['KIA NIRO EV', 'KIA NIRO PLUS'], 'BEV'),
    ('Kia', 'EV6', ['KIA EV6'], 'BEV'),
    ('Hyundai', 'Ioniq 5', ['HYUNDAI IONIQ 5', 'HYUNDAI IONIQ5'], 'BEV'),
    ('Hyundai', 'Kona EV', ['HYUNDAI KONA EV', 'HYUNDAI KONA ELECTRIC'], 'BEV'),
    ('Volkswagen', 'ID.4', ['VOLKSWAGEN ID 4', 'VW ID 4'], 'BEV'),
    ('Volkswagen', 'ID.3', ['VOLKSWAGEN ID 3', 'VW ID 3'], 'BEV'),
    ('Volkswagen', 'ID.Buzz', ['VOLKSWAGEN ID BUZZ', 'VW ID BUZZ'], 'BEV'),
    ('Toyota', 'Corolla Cross Hybrid', ['TOYOTA COROLLA CROSS HEV', 'TOYOTA COROLLA CROSS HYBRID', 'TOYOTA COROLLA CROSS HIBRIDO'], 'HEV'),
    ('Toyota', 'Corolla Hybrid', ['TOYOTA COROLLA HEV', 'TOYOTA COROLLA HYBRID', 'TOYOTA COROLLA HIBRIDO', 'TOYOTA COROLLA ALTIS HEV', 'TOYOTA COROLLA ALTIS HYBRID'], 'HEV'),
    ('Toyota', 'Prius', ['TOYOTA PRIUS'], 'HEV'),
    ('Toyota', 'Camry Hybrid', ['TOYOTA CAMRY HEV', 'TOYOTA CAMRY HYBRID'], 'HEV'),
    ('Toyota', 'RAV4 Hybrid', ['TOYOTA RAV4 HEV', 'TOYOTA RAV4 HYBRID'], 'HEV'),
    ('Toyota', 'RAV4 Plug-in', ['TOYOTA RAV4 PHEV', 'TOYOTA RAV4 PRIME'], 'PHEV'),
    ('Toyota', 'Yaris Hybrid', ['TOYOTA YARIS HEV', 'TOYOTA YARIS HYBRID'], 'HEV'),
    ('Lexus', 'NX Hybrid', ['LEXUS NX HEV', 'LEXUS NX HYBRID', 'LEXUS NX 350H', 'LEXUS NX350H'], 'HEV'),
    ('Lexus', 'UX Hybrid', ['LEXUS UX HEV', 'LEXUS UX HYBRID', 'LEXUS UX 250H', 'LEXUS UX250H'], 'HEV'),
    ('Honda', 'City Hybrid', ['HONDA CITY HEV', 'HONDA CITY HYBRID', 'HONDA CITY HIBRIDO'], 'HEV'),
    ('Honda', 'Civic Hybrid', ['HONDA CIVIC HEV', 'HONDA CIVIC HYBRID', 'HONDA CIVIC E HEV'], 'HEV'),
    ('Honda', 'HR-V Hybrid', ['HONDA HRV HEV', 'HONDA HR V HEV', 'HONDA HRV HYBRID', 'HONDA HR V HYBRID'], 'HEV'),
    ('Honda', 'Accord Hybrid', ['HONDA ACCORD HEV', 'HONDA ACCORD HYBRID', 'HONDA ACCORD E HEV'], 'HEV'),
    ('Ford', 'Territory Hybrid', ['FORD TERRITORY HEV', 'FORD TERRITORY HYBRID'], 'HEV'),
    ('Kia', 'Sportage Hybrid', ['KIA SPORTAGE HEV', 'KIA SPORTAGE HYBRID'], 'HEV'),
    ('Kia', 'Sportage Plug-in', ['KIA SPORTAGE PHEV'], 'PHEV'),
    ('Kia', 'Niro Hybrid', ['KIA NIRO HEV', 'KIA NIRO HYBRID'], 'HEV'),
    ('Hyundai', 'Tucson Hybrid', ['HYUNDAI TUCSON HEV', 'HYUNDAI TUCSON HYBRID'], 'HEV'),
    ('Hyundai', 'Tucson Plug-in', ['HYUNDAI TUCSON PHEV'], 'PHEV'),
    ('Hyundai', 'Kona Hybrid', ['HYUNDAI KONA HEV', 'HYUNDAI KONA HYBRID'], 'HEV'),
    ('Mitsubishi', 'Outlander PHEV', ['MITSUBISHI OUTLANDER PHEV', 'MMC OUTLANDER PHEV'], 'PHEV'),
    ('Volvo', 'XC60 T8', ['VOLVO XC60 T8', 'VOLVO XC60 PHEV', 'VOLVO XC60 RECHARGE'], 'PHEV'),
    ('Volvo', 'XC90 T8', ['VOLVO XC90 T8', 'VOLVO XC90 PHEV', 'VOLVO XC90 RECHARGE'], 'PHEV'),
    ('BMW', 'X1 PHEV', ['BMW X1 PHEV', 'BMW X1 30E', 'BMW X1 XDRIVE30E'], 'PHEV'),
    ('BMW', 'X3 PHEV', ['BMW X3 PHEV', 'BMW X3 30E', 'BMW X3 XDRIVE30E'], 'PHEV'),
    ('Jeep', 'Compass 4xe', ['JEEP COMPASS 4XE', 'JEEP COMPASS PHEV'], 'PHEV'),
    ('Jeep', 'Commander 4xe', ['JEEP COMMANDER 4XE', 'JEEP COMMANDER PHEV'], 'PHEV'),
    ('Land Rover', 'Range Rover PHEV', ['LAND ROVER RANGE ROVER PHEV', 'RANGE ROVER PHEV', 'RANGE ROVER SPORT PHEV'], 'PHEV'),
    ('Porsche', 'Cayenne E-Hybrid', ['PORSCHE CAYENNE E HYBRID', 'PORSCHE CAYENNE PHEV'], 'PHEV'),
    ('Audi', 'Q5 TFSI e', ['AUDI Q5 TFSI E', 'AUDI Q5 PHEV'], 'PHEV'),
    ('Mercedes-Benz', 'GLC 300e', ['MERCEDES BENZ GLC 300E', 'MERCEDES GLC 300E', 'MERCEDES GLC PHEV'], 'PHEV'),
    ('Toyota', 'Corolla Cross HEV Flex', ['TOYOTA COROLLA CROSS HEV FLEX'], 'HEV_FLEX'),
    ('BYD', 'Shark', ['BYD SHARK'], 'PHEV'),
    ('BYD', 'Song L', ['BYD SONG L'], 'BEV'),
    ('BYD', 'Sealion 05', ['BYD SEALION 05', 'BYD SEALION05'], 'PHEV'),
    ('BYD', 'Sealion 06', ['BYD SEALION 06', 'BYD SEALION06'], 'BEV'),
    ('Citroën', 'ë-C3', ['CITROEN E C3', 'CITROEN EC3', 'CITROEN C3 ELETRICO'], 'BEV'),
    ('Peugeot', 'E-2008', ['PEUGEOT E 2008', 'PEUGEOT E2008'], 'BEV'),
    ('BMW', 'i5', ['BMW I5'], 'BEV'),
    ('BMW', 'i7', ['BMW I7'], 'BEV'),
    ('Hyundai', 'Ioniq 6', ['HYUNDAI IONIQ 6', 'HYUNDAI IONIQ6'], 'BEV'),
    ('Kia', 'EV9', ['KIA EV9'], 'BEV'),
    ('Volkswagen', 'ID.7', ['VOLKSWAGEN ID 7', 'VW ID 7'], 'BEV'),
    ('Audi', 'Q4 e-tron', ['AUDI Q4 E TRON', 'AUDI Q4 ETRON'], 'BEV'),
    ('Mercedes-Benz', 'EQE', ['MERCEDES BENZ EQE', 'MERCEDES EQE'], 'BEV'),
    ('Jeep', 'Renegade 4xe', ['JEEP RENEGADE 4XE', 'JEEP RENEGADE PHEV'], 'PHEV'),
    ('Nissan', 'Kicks e-Power', ['NISSAN KICKS E POWER', 'NISSAN KICKS EPOWER'], 'HEV'),
    ('Toyota', 'Hilux Hybrid', ['TOYOTA HILUX HEV', 'TOYOTA HILUX HYBRID', 'TOYOTA HILUX HIBRIDO'], 'HEV'),
    ('Honda', 'ZR-V Hybrid', ['HONDA ZR V HEV', 'HONDA ZRV HEV', 'HONDA ZR V HYBRID', 'HONDA ZRV HYBRID'], 'HEV'),
    ('Chevrolet', 'Equinox EV', ['CHEVROLET EQUINOX EV', 'GM EQUINOX EV'], 'BEV'),
    ('Ford', 'Mustang Mach-E', ['FORD MUSTANG MACH E', 'FORD MACH E'], 'BEV'),
    ('Volvo', 'C40 Recharge', ['VOLVO C40 RECHARGE', 'VOLVO C40 BEV'], 'BEV'),
    ('Lexus', 'RX Hybrid', ['LEXUS RX HEV', 'LEXUS RX HYBRID', 'LEXUS RX 450H', 'LEXUS RX450H'], 'HEV'),
    ('Toyota', 'SW4 Hybrid', ['TOYOTA SW4 HEV', 'TOYOTA SW4 HYBRID', 'TOYOTA SW4 HIBRIDO'], 'HEV'),
    ('Caoa Chery', 'eQ5', ['CAOA CHERY EQ5', 'CHERY EQ5'], 'BEV'),
    ('Porsche', 'Macan Electric', ['PORSCHE MACAN ELECTRIC', 'PORSCHE MACAN EV'], 'BEV'),
]

HINT_TOKENS = {'HEV', 'PHEV', 'MHEV', 'HYBRID', 'HIBRIDO', 'HIBRIDA', 'ELETRICO', 'ELETRICA', 'ELECTRIC', 'BEV', 'EV', 'DM', 'ETECH'}

def _prefix_index():
    items = []
    for brand, model, prefixes, propulsion in FAMILIES:
        for prefix in prefixes:
            items.append((prefix, brand, model, propulsion))
    items.sort(key=lambda item: len(item[0]), reverse=True)
    return tuple(items)

PREFIXES = _prefix_index()

def electrified_hint(name):
    from src.matching import normalize
    text = normalize(name)
    tokens = set(text.split())
    if tokens & HINT_TOKENS:
        return True
    return any(marker in text for marker in ('E TECH', 'PLUG IN', 'FONTE EXTERNA', 'FONTE INTERNA', '4XE', 'TFSI E'))

@lru_cache(maxsize=100000)
def classify(name):
    from src.matching import normalize
    n = normalize(name)
    for prefix, brand, model, propulsion in PREFIXES:
        if n == prefix or n.startswith(prefix + ' '):
            return brand, model, n[len(prefix):].strip() or 'Não informada', propulsion
    return None

def propulsion_of(name):
    found = classify(name)
    return found[3] if found else None

TECHNICAL_RULES = [
    dict(model_id='byd-dolphin-gs-2026', battery_id='byd-dolphin-gs-2026-r1', brand='BYD', model='Dolphin', version='GS',
         year_start=2026, year_end=2026, aliases=['BYD DOLPHIN GS', 'BYD DOLPHIN GS 180EV'], propulsion='BEV',
         capacity_kwh=44.9, chemistry=None, technology='Blade', pack_manufacturer='BYD', warranty_years=8,
         warranty_km=200000, part_number=None, confidence='A', source=PBE, url=OEM,
         notes='Estimativa pelo ano do veículo. Condições de uso e início efetivo devem ser conferidos no manual aplicável.'),
    dict(model_id='byd-dolphin-mini-38', battery_id='byd-dolphin-mini-38-r1', brand='BYD', model='Dolphin Mini', version='Não informada',
         year_start=2023, year_end=2026, aliases=['BYD DOLPHIN MINI'], propulsion='BEV', capacity_kwh=38.0, chemistry=None,
         technology='Blade', pack_manufacturer='BYD', warranty_years=8, warranty_km=150000, part_number=None, confidence='B',
         source=PBE, url='https://www.byd.com/br', notes='Pack típico do Dolphin Mini no BR (~38 kWh). Não extrapolar para Dolphin GS.'),
    dict(model_id='gwm-ora-03-48', battery_id='gwm-ora-03-48-r1', brand='GWM', model='Ora 03', version='Não informada',
         year_start=2023, year_end=2026, aliases=['GWM ORA 03', 'GWM ORA03'], propulsion='BEV', capacity_kwh=48.0, chemistry=None,
         technology=None, pack_manufacturer='GWM', warranty_years=8, warranty_km=150000, part_number=None, confidence='B',
         source=PBE, url=GWM, notes='Capacidade típica publicada para Ora 03 no BR; versões 400/GT podem diferir.'),
    dict(model_id='renault-kwid-etech-26', battery_id='renault-kwid-etech-26-r1', brand='Renault', model='Kwid E-Tech', version='Não informada',
         year_start=2022, year_end=2026, aliases=['RENAULT E KWID', 'RENAULT KWID E TECH', 'RENAULT KWID ETECH'], propulsion='BEV',
         capacity_kwh=26.8, chemistry=None, technology=None, pack_manufacturer='Renault', warranty_years=8,
         warranty_km=160000, part_number=None, confidence='B', source=PBE, url=RENAULT,
         notes='Capacidade nominal divulgada para Kwid E-Tech; confirmar versão.'),
    dict(model_id='nissan-leaf-40', battery_id='nissan-leaf-40-r1', brand='Nissan', model='Leaf', version='Não informada',
         year_start=2019, year_end=2026, aliases=['NISSAN LEAF'], propulsion='BEV', capacity_kwh=40.0, chemistry='NMC',
         technology=None, pack_manufacturer='Nissan', warranty_years=8, warranty_km=160000, part_number=None,
         confidence='B', source=PBE, url=NISSAN, notes='Pacote 40 kWh mais comum no BR; versões 62 kWh ficam pendentes.'),
    dict(model_id='volvo-ex30-51', battery_id='volvo-ex30-51-r1', brand='Volvo', model='EX30', version='Não informada',
         year_start=2024, year_end=2026, aliases=['VOLVO EX30'], propulsion='BEV', capacity_kwh=51.0, chemistry=None,
         technology=None, pack_manufacturer='Volvo', warranty_years=8, warranty_km=160000, part_number=None,
         confidence='B', source=PBE, url=VOLVO, notes='Capacidade do pack Single Motor; Twin Motor usa pack maior.'),
    dict(model_id='fiat-500e-42', battery_id='fiat-500e-42-r1', brand='Fiat', model='500e', version='Não informada',
         year_start=2022, year_end=2026, aliases=['FIAT 500E'], propulsion='BEV', capacity_kwh=42.0, chemistry=None,
         technology=None, pack_manufacturer='Stellantis', warranty_years=8, warranty_km=160000, part_number=None,
         confidence='B', source=PBE, url=FIAT, notes='Capacidade nominal divulgada para 500e.'),
    dict(model_id='tesla-model3-60', battery_id='tesla-model3-60-r1', brand='Tesla', model='Model 3', version='Não informada',
         year_start=2021, year_end=2026, aliases=['TESLA MODEL 3'], propulsion='BEV', capacity_kwh=60.0, chemistry=None,
         technology=None, pack_manufacturer='Tesla', warranty_years=8, warranty_km=160000, part_number=None,
         confidence='B', source=PBE, url=TESLA, notes='Pacote de entrada aproximado; Long Range/Performance diferem.'),
    dict(model_id='byd-song-plus-dmi-18', battery_id='byd-song-plus-dmi-18-r1', brand='BYD', model='Song Plus', version='DM-i',
         year_start=2023, year_end=2026, aliases=['BYD SONG PLUS', 'BYD SONG PLUS DM I'], propulsion='PHEV',
         capacity_kwh=18.3, chemistry=None, technology='Blade', pack_manufacturer='BYD', warranty_years=8,
         warranty_km=150000, part_number=None, confidence='B', source=PBE, url='https://www.byd.com/br',
         notes='Pack híbrido plug-in típico do Song Plus DM-i no BR.'),
    dict(model_id='byd-king-dmi-18', battery_id='byd-king-dmi-18-r1', brand='BYD', model='King', version='DM-i',
         year_start=2024, year_end=2026, aliases=['BYD KING', 'BYD KING DM I'], propulsion='PHEV',
         capacity_kwh=18.3, chemistry=None, technology='Blade', pack_manufacturer='BYD', warranty_years=8,
         warranty_km=150000, part_number=None, confidence='B', source=PBE, url='https://www.byd.com/br',
         notes='Pack híbrido plug-in típico do King DM-i.'),
    dict(model_id='gwm-h6-phev34-2025', battery_id='gwm-h6-phev34-2025-r1', brand='GWM', model='Haval H6 PHEV', version='PHEV34',
         year_start=2025, year_end=2025, aliases=['GWM HAVAL H6 PHEV', 'HAVAL H6 PHEV'], propulsion='PHEV',
         capacity_kwh=34.0, chemistry=None, technology=None, pack_manufacturer='GWM', warranty_years=8,
         warranty_km=150000, part_number=None, confidence='A', source=PBE,
         url='https://www.gwmmotors.com.br/content/dam/gwm/pages/br/pt/models/haval-h6-phev-34-2025/FichaTecnica_Haval_H6_PHEV34_2025.pdf',
         notes='Ficha GWM Haval H6 PHEV34 2025: 34 kWh. Não extrapolar para outras versões.'),
    dict(model_id='toyota-corolla-hybrid-14', battery_id='toyota-corolla-hybrid-14-r1', brand='Toyota', model='Corolla Hybrid', version='Não informada',
         year_start=2020, year_end=2026, aliases=['TOYOTA COROLLA HEV', 'TOYOTA COROLLA HYBRID', 'TOYOTA COROLLA ALTIS HEV'],
         propulsion='HEV', capacity_kwh=1.3, chemistry='NiMH', technology=None, pack_manufacturer='Primearth EV Energy',
         warranty_years=8, warranty_km=160000, part_number=None, confidence='B', source=PBE, url=TOYOTA,
         notes='Bateria de tração híbrida (~1,3 kWh NiMH). Não confundir com tanque de combustível.'),
    dict(model_id='toyota-corolla-cross-hybrid-14', battery_id='toyota-corolla-cross-hybrid-14-r1', brand='Toyota',
         model='Corolla Cross Hybrid', version='Não informada', year_start=2021, year_end=2026,
         aliases=['TOYOTA COROLLA CROSS HEV', 'TOYOTA COROLLA CROSS HYBRID'], propulsion='HEV', capacity_kwh=1.4,
         chemistry='Li-ion', technology=None, pack_manufacturer='Toyota', warranty_years=8, warranty_km=160000,
         part_number=None, confidence='B', source=PBE, url=TOYOTA, notes='Pack híbrido do Corolla Cross; capacidade pequena, alta cobertura de frota.'),
    dict(model_id='honda-city-hybrid-08', battery_id='honda-city-hybrid-08-r1', brand='Honda', model='City Hybrid', version='Não informada',
         year_start=2023, year_end=2026, aliases=['HONDA CITY HEV', 'HONDA CITY HYBRID', 'HONDA CITY HIBRIDO'], propulsion='HEV',
         capacity_kwh=0.9, chemistry='Li-ion', technology=None, pack_manufacturer='Honda', warranty_years=8,
         warranty_km=160000, part_number=None, confidence='B', source=PBE, url=HONDA,
         notes='Pack compacto e:HEV. Valores nominais aproximados da ficha.'),
]

def seed_catalog():
    with engine.begin() as c:
        if not c.execute(select(sources.c.id).where(sources.c.id == 'senatran')).first():
            c.execute(insert(sources), [
                dict(id='senatran', name='SENATRAN / RENAVAM', url=CATALOG, kind='oficial', collected_at=None,
                     details={'scope': 'Recorte de famílias eletrificadas validadas; comparar com a frota oficial por combustível.'}),
                dict(id='inmetro', name='INMETRO / PBE Veicular', url=PBE, kind='oficial', collected_at=now(),
                     details={'role': 'Classificação e eficiência de eletrificados etiquetados'}),
                dict(id='byd', name='BYD — ficha Dolphin GS, julho/2026', url=OEM, kind='fabricante', collected_at=now(),
                     details={'role': 'Especificação por versão; outros anos precisam de revisão'}),
                dict(id='openev', name='OpenEV Data', url='https://github.com/open-ev-data/open-ev-data-dataset', kind='tecnica', collected_at=None,
                     details={'role': 'Sugestões técnicas; não aplicar garantias globais automaticamente ao Brasil'}),
                dict(id='fenabrave', name='Fenabrave — emplacamentos', url='https://www.fenabrave.org.br/', kind='setorial', collected_at=None,
                     details={'role': 'Emplacamentos mensais; não confundir com estoque SENATRAN'}),
                dict(id='ibge-pop', name='IBGE — população', url='https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado', kind='oficial', collected_at=None,
                     details={'role': 'População por UF para densidade de eletrificados'}),
            ])
        else:
            for row in [
                dict(id='fenabrave', name='Fenabrave — emplacamentos', url='https://www.fenabrave.org.br/', kind='setorial', collected_at=None,
                     details={'role': 'Emplacamentos mensais; não confundir com estoque SENATRAN'}),
                dict(id='ibge-pop', name='IBGE — população', url='https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado', kind='oficial', collected_at=None,
                     details={'role': 'População por UF para densidade de eletrificados'}),
            ]:
                if not c.execute(select(sources.c.id).where(sources.c.id == row['id'])).first():
                    c.execute(insert(sources).values(**row))
        existing_models = {r['id']: dict(r) for r in c.execute(select(models)).mappings()}
        existing_batteries = set(c.execute(select(batteries.c.id)).scalars())
        for rule in TECHNICAL_RULES:
            if rule['model_id'] not in existing_models:
                payload = dict(id=rule['model_id'], brand=rule['brand'], model=rule['model'], version=rule['version'],
                               year_start=rule['year_start'], year_end=rule['year_end'], classification_source=rule['source'],
                               aliases=rule['aliases'], category=rule['propulsion'])
                if 'propulsion' in models.c:
                    payload['propulsion'] = rule['propulsion']
                c.execute(insert(models).values(**payload))
                existing_models[rule['model_id']] = payload
            else:
                current = existing_models[rule['model_id']]
                merged = list(dict.fromkeys(list(current.get('aliases') or []) + list(rule['aliases'])))
                values = {'aliases': merged}
                if 'propulsion' in models.c:
                    values['propulsion'] = rule['propulsion']
                c.execute(models.update().where(models.c.id == rule['model_id']).values(**values))
            if rule['battery_id'] not in existing_batteries:
                payload = dict(id=rule['battery_id'], model_id=rule['model_id'], capacity_kwh=rule['capacity_kwh'],
                               usable_kwh=None, chemistry=rule['chemistry'], technology=rule['technology'],
                               pack_manufacturer=rule['pack_manufacturer'], warranty_years=rule['warranty_years'],
                               warranty_km=rule['warranty_km'],
                               warranty_notes=rule['notes'],
                               provenance={'capacity_kwh': rule['url'], 'technology': rule['url'], 'warranty_years': rule['url']},
                               confidence=rule['confidence'])
                if 'part_number' in batteries.c:
                    payload['part_number'] = rule['part_number']
                c.execute(insert(batteries).values(**payload))
                existing_batteries.add(rule['battery_id'])
    from sqlalchemy import text
    with engine.begin() as c:
        try:
            pending = list(c.execute(text('SELECT DISTINCT raw_name FROM frota_snapshot WHERE propulsion IS NULL')).scalars())
        except Exception:
            pending = []
        for name in pending:
            found = classify(name)
            if found:
                c.execute(text('UPDATE frota_snapshot SET propulsion=:p WHERE raw_name=:n AND propulsion IS NULL'), {'p': found[3], 'n': name})
        try:
            pending_batt = list(c.execute(text('SELECT DISTINCT raw_name, vehicle_year FROM frota_snapshot WHERE battery_id IS NULL')).mappings())
            cat = [dict(r) for r in c.execute(select(models)).mappings()]
            battery = {r.model_id: r.id for r in c.execute(select(batteries)).mappings()}
        except Exception:
            pending_batt, cat, battery = [], [], {}
        from src.matching import match
        for row in pending_batt:
            result = match(row['raw_name'], row['vehicle_year'], cat)
            model = result.get('model')
            if not model:
                continue
            battery_id = battery.get(model['id'])
            if not battery_id:
                continue
            c.execute(text('UPDATE frota_snapshot SET battery_id=:b, confidence=:c, match_method=:m WHERE raw_name=:n AND battery_id IS NULL AND ((vehicle_year IS NULL AND :y IS NULL) OR vehicle_year=:y)'),
                      {'b': battery_id, 'c': result['confidence'], 'm': result['method'], 'n': row['raw_name'], 'y': row['vehicle_year']})
    from src.connectors.reviewed_sources import seed_reviewed
    seed_reviewed()
