from datetime import date, datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

Powertrain=Literal['BEV','PHEV','HEV','HEV_FLEX','MHEV_12V','MHEV_48V','FCEV','ELECTRIFIED','UNKNOWN']
Category=Literal['bus','light','truck','motorcycle','unknown']
Confidence=Literal['high','medium','low','unknown']
Status=Literal['unknown','automotive_first_life','removed','evaluation','second_life','stationary_storage','remanufactured','recycling','retired']
class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True,allow_inf_nan=False)
    @field_validator('*',mode='before')
    @classmethod
    def empty(cls,v): return None if v=='' else v

class Source(Strict):
    provider:str=Field(min_length=2,max_length=100)
    dataset_name:str=Field(min_length=2,max_length=150)
    reference_period:str=Field(pattern=r'^20\d{2}-(?:0[1-9]|1[0-2]|H[12])$')
    method:Literal['API','download','import','review']='import'
    source_url:str=Field(pattern=r'^https?://',max_length=2000)
    license_or_terms:str=Field(min_length=3,max_length=1000)
    scope:str=Field(min_length=5,max_length=1000)
    metric:Literal['fleet_stock','registrations','cumulative_registrations','technical','individual']
    notes:str|None=Field(None,max_length=2000)
    access_level:Literal['public','restricted']='public'

class Observation(Strict):
    country:str='BR'
    state:str|None=Field(None,pattern=r'^[A-Z]{2}$')
    municipality:str|None=None
    vehicle_category:Category='unknown'
    manufacturer:str|None=None
    model:str|None=None
    trim:str|None=None
    model_year:int|None=Field(None,ge=1900,le=date.today().year+1)
    manufacture_year:int|None=Field(None,ge=1900,le=date.today().year+1)
    powertrain_type:Powertrain='UNKNOWN'
    fuel_type:str|None=None
    quantity:int=Field(ge=0,le=100_000_000,strict=True)
    source_record_reference:str=Field(min_length=1,max_length=250)
    confidence:Confidence='unknown'

class Import(Strict):
    source:Source
    observations:list[Observation]=Field(min_length=1,max_length=10000)
    @model_validator(mode='after')
    def unique(self):
        keys=[r.source_record_reference for r in self.observations]
        if len(keys)!=len(set(keys)):raise ValueError('Linhas de origem duplicadas')
        if self.source.access_level!='public' or self.source.metric in ('technical','individual'):raise ValueError('Importação agregada exige fonte pública de quantidades')
        return self

class Provenance(Strict):
    source_snapshot_id:str

class Spec(Provenance):
    manufacturer:str
    model:str
    trim:str|None=None
    model_year_start:int|None=Field(None,ge=1900,le=2100)
    model_year_end:int|None=Field(None,ge=1900,le=2100)
    powertrain:Powertrain='UNKNOWN'
    country_market:str='BR'
    vehicle_category:Category='unknown'
    @model_validator(mode='after')
    def years(self):
        if self.model_year_start and self.model_year_end and self.model_year_start>self.model_year_end:raise ValueError('Vigência invertida')
        return self

class BatteryModel(Provenance):
    manufacturer:str|None=None
    model_name:str|None=None
    chemistry:str|None=None
    technology:str|None=None
    part_number:str|None=None
    nominal_capacity_kwh:float|None=Field(None,gt=0,le=3000)
    usable_capacity_kwh:float|None=Field(None,gt=0,le=3000)
    nominal_voltage_v:float|None=Field(None,gt=0,le=3000)
    min_voltage_v:float|None=Field(None,gt=0,le=3000)
    max_voltage_v:float|None=Field(None,gt=0,le=3000)
    rated_capacity_ah:float|None=Field(None,gt=0)
    expected_cycle_life:float|None=Field(None,gt=0)
    automotive_eol_threshold_percent:float|None=Field(None,ge=0,le=100)
    warranty_years:float|None=Field(None,ge=0,le=50)
    warranty_km:float|None=Field(None,ge=0)
    module_count:int|None=Field(None,gt=0)
    cell_count:int|None=Field(None,gt=0)
    confidence:Confidence='unknown'
    field_sources:dict[str,str]=Field(default_factory=dict)
    @model_validator(mode='after')
    def capacity(self):
        if self.usable_capacity_kwh and self.nominal_capacity_kwh and self.usable_capacity_kwh>self.nominal_capacity_kwh:raise ValueError('Capacidade útil maior que nominal')
        for v in self.field_sources.values():
            if not v.startswith(('https://','http://')):raise ValueError('Fonte por campo deve ser URL HTTP')
        return self

class Evidence(Provenance):
    vehicle_spec_id:str
    battery_model_id:str
    valid_from:date|None=None
    valid_to:date|None=None
    confidence:Confidence='unknown'
    evidence_type:Literal['oem','technical','partner','manual']
    status:Literal['candidate','accepted','rejected']='candidate'
    notes:str=Field(min_length=10,max_length=2000)
    @model_validator(mode='after')
    def interval(self):
        if self.valid_from and self.valid_to and self.valid_from>self.valid_to:raise ValueError('Vigência invertida')
        return self

class Vehicle(Provenance):
    vin:str|None=Field(None,pattern=r'^[A-HJ-NPR-Z0-9]{17}$')
    renavam:str|None=Field(None,pattern=r'^\d{9,11}$')
    plate:str|None=Field(None,pattern=r'^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$')
    manufacturer:str|None=None
    model:str|None=None
    trim:str|None=None
    model_year:int|None=Field(None,ge=1900,le=date.today().year+1)
    manufacture_year:int|None=Field(None,ge=1900,le=date.today().year+1)
    vehicle_category:Category='unknown'
    powertrain_type:Powertrain='UNKNOWN'
    registration_date:date|None=None
    state:str|None=Field(None,pattern=r'^[A-Z]{2}$')
    municipality:str|None=None
    operator:str|None=None
    identity_status:Literal['aggregate_derived','partially_identified','identified','verified','retired','unknown']='unknown'
    identity_confidence:Confidence='unknown'
    vehicle_spec_id:str|None=None
    @model_validator(mode='after')
    def identity(self):
        if self.identity_status=='aggregate_derived':raise ValueError('Use a materialização de observação para slots')
        if self.identity_status in ('identified','verified') and not any((self.vin,self.renavam,self.plate)):raise ValueError('Identidade requer identificador real')
        return self

class BatteryAsset(Provenance):
    battery_model_id:str|None=None
    parent_battery_id:str|None=None
    passport_identifier:str|None=None
    manufacturer_serial_number:str|None=None
    manufacturing_date:date|None=None
    first_service_date:date|None=None
    current_status:Status='unknown'
    confidence:Confidence='unknown'
    battery_identifier_type:str|None=None
    battery_identifier:str|None=None
    passport_schema:str|None=None
    passport_version:str|None=None
    passport_url_reference:str|None=Field(None,pattern=r'^https?://')
    passport_issuer:str|None=None
    passport_created_at:datetime|None=None
    passport_last_updated:datetime|None=None

class Installation(Provenance):
    vehicle_id:str
    battery_id:str
    position:str=Field('traction-1',min_length=1,max_length=50)
    installed_at:date
    removed_at:date|None=None
    installation_type:Literal['original','replacement','unknown']='unknown'
    confidence:Confidence='unknown'
    @model_validator(mode='after')
    def dates(self):
        if self.removed_at and self.removed_at<self.installed_at:raise ValueError('Remoção anterior à instalação')
        return self

class Lifecycle(Provenance):
    battery_id:str
    observed_at:datetime
    event_type:Literal['measurement','maintenance','accident','removal','evaluation','repurpose','remanufacture','recycling']
    current_status:Status|None=None
    soh_percent:float|None=Field(None,ge=0,le=100)
    soce_percent:float|None=Field(None,ge=0,le=100)
    soc_percent:float|None=Field(None,ge=0,le=100)
    odometer_km:float|None=Field(None,ge=0)
    energy_throughput_kwh:float|None=Field(None,ge=0)
    temperature_c:float|None=Field(None,ge=-100,le=300)
    cycle_count:int|None=Field(None,ge=0)
    fast_charge_count:int|None=Field(None,ge=0)
    notes:str|None=None
    @field_validator('observed_at')
    @classmethod
    def timezone(cls,v):
        if v.tzinfo is None:raise ValueError('Informe fuso horário na observação')
        return v.astimezone(timezone.utc)

class Forecast(Strict):
    vehicle_year:int|None=Field(None,ge=1900,le=date.today().year+1)
    registration_date:date|None=None
    vehicle_category:Category='unknown'
    battery_model:str|None=None
    earliest_age:int=Field(8,ge=1,le=50)
    most_likely_age:int=Field(10,ge=1,le=50)
    latest_age:int=Field(12,ge=1,le=50)
    residual_fraction:float|None=Field(None,ge=0,le=1)
    @model_validator(mode='after')
    def ordered(self):
        if not self.earliest_age<=self.most_likely_age<=self.latest_age:raise ValueError('Janelas devem estar ordenadas')
        return self

MODELS={'vehicle-specs':Spec,'battery-models':BatteryModel,'evidence':Evidence,'vehicles':Vehicle,'battery-assets':BatteryAsset,'installations':Installation,'lifecycle':Lifecycle}
