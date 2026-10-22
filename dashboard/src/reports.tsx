import { useEffect } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowUpRight, Clock3, Database, ExternalLink, MapPin, SlidersHorizontal, X } from 'lucide-react';
import { BrazilMap } from './brazil-map';
import { useResource, type Page, type Publication } from './api';
import { Badge, electrification, Empty, ErrorState, Heading, Loading, month, Note, number, Pager, SelectFilter, TextFilter, useFilters, vehicleType } from './ui';

interface Observation { id: string; state?: string; municipality?: string; manufacturer?: string; model: string; trim?: string; powertrain_type?: string; vehicle_category?: string; quantity: number; reference_period?: string; model_id?: string; manufacture_year?: number; }
interface HeavyStatistic { id: string; state?: string; municipality?: string; vehicle_type?: string; species?: string; axles?: number; quantity: number; }
interface Facets {
  states: string[];
  state_quantities: Record<string, number>;
  municipalities: string[];
  manufacturers: string[];
  models: string[];
  vehicle_types: string[];
  powertrains: string[];
}
interface PublicationsResponse { items: Publication[]; active?: Publication; expected_through?: string; missing_references?: string[]; official_source?: string; }
export function ReportsPage() {
  const filters = useFilters(); const { params, offset, update, setOffset } = filters; const [, setParams] = useSearchParams();
  const pubs = useResource<PublicationsResponse>('/publications');
  const query = new URLSearchParams(params); query.set('limit', '50'); query.set('offset', String(offset));
  const result = useResource<Page<Observation> & { total_quantity?: number; coverage?: string }>('/observations?' + query);
  const facetsQuery = new URLSearchParams(); ['publication_id', 'reference', 'uf', 'manufacturer', 'model', 'vehicle_type', 'powertrain_type'].forEach(key => { if (params.get(key)) facetsQuery.set(key, params.get(key)!); });
  const facets = useResource<Facets>('/observation-filters?' + facetsQuery);
  const heavyQuery = new URLSearchParams({ limit: '20' });
  const heavyReference = result.data?.publication?.reference_period || params.get('reference');
  if (heavyReference) heavyQuery.set('reference', heavyReference);
  if (params.get('uf')) heavyQuery.set('uf', params.get('uf')!);
  if (params.get('city')) heavyQuery.set('municipality', params.get('city')!);
  const heavy = useResource<Page<HeavyStatistic> & { total_quantity?: number; notice?: string }>('/heavy-statistics?' + heavyQuery);
  useEffect(() => { if (result.data?.publication?.id && !params.has('publication_id') && !params.has('reference')) setParams(current => { const next = new URLSearchParams(current); next.set('publication_id', result.data!.publication!.id); return next; }, { replace: true }); }, [result.data?.publication?.id, params, setParams]);
  const selectState = (uf: string) => setParams(current => { const next = new URLSearchParams(current); next.delete('city'); next.delete('offset'); next.set('uf', uf); return next; }, { replace: true });
  const typeFilter = params.get('vehicle_type');
  const powertrainFilter = params.get('powertrain_type');
  const activeFilters = [
    ['uf', params.get('uf')], ['city', params.get('city')], ['manufacturer', params.get('manufacturer')],
    ['model', params.get('model')], ['manufacture_year', params.get('manufacture_year')], ['vehicle_type', typeFilter ? vehicleType(typeFilter) : ''],
    ['powertrain_type', powertrainFilter ? electrification(powertrainFilter) : ''],
  ].filter((entry): entry is [string, string] => Boolean(entry[1]));
  return <>
    <Heading eyebrow="ESTATÍSTICAS MENSAIS" title="Dados da frota" description="Explore as quantidades publicadas por modelo, município e estado." />
    <Note>Esta consulta mostra quantidades agregadas. O município de uma estatística não é a localização individual de uma ficha pendente.</Note>
    {!!pubs.data?.missing_references?.length && <div className="source-status" aria-live="polite"><Clock3 size={19} /><div><strong>{pubs.data.missing_references.map(month).join(', ')} — base de marca/modelo ainda não importada</strong><span>Escolha o mês em Administração para baixar e importar os relatórios disponíveis.</span></div>{pubs.data.official_source && <a href={pubs.data.official_source} target="_blank" rel="noreferrer">Ver fonte<ExternalLink size={14} /></a>}</div>}
    <section className="report-explorer" aria-label="Filtros dos dados da frota">
      <div className="report-explorer-title"><div><SlidersHorizontal size={18} /><span><strong>Explore a frota</strong><small>Escolha um estado no mapa ou combine os campos.</small></span></div>{activeFilters.length > 0 && <button className="text-button" onClick={filters.clear}><X size={14} />Limpar {activeFilters.length} {activeFilters.length === 1 ? 'filtro' : 'filtros'}</button>}</div>
      <div className="report-filter-layout">
        <div className="geo-filter">
          <div className="geo-filter-heading"><MapPin size={17} /><span><strong>{params.get('uf') ? `Estado: ${params.get('uf')}` : 'Brasil inteiro'}</strong><small>Clique em uma UF para filtrar</small></span></div>
          <BrazilMap selected={params.get('uf') || undefined} available={facets.data?.states} quantities={facets.data?.state_quantities} onSelect={selectState} />
          {params.get('uf') && <button className="map-reset" onClick={() => update('uf', '')}>Mostrar todo o Brasil</button>}
        </div>
        <div className="report-filter-fields">
          <label className="field field-wide"><span>Competência mensal</span><select value={params.get('publication_id') || pubs.data?.items.find(pub => pub.reference_period === params.get('reference'))?.id || ''} onChange={e => { setParams(current => { const next = new URLSearchParams(current); next.delete('reference'); next.delete('offset'); if (e.target.value) next.set('publication_id', e.target.value); else next.delete('publication_id'); return next; }); }}><option value="">Publicação mais recente</option>{pubs.data?.items.map(pub => <option key={pub.id} value={pub.id}>{month(pub.reference_period)}</option>)}</select></label>
          {facets.data?.municipalities.length ? <SelectFilter {...filters} name="city" label="Município / cidade" options={facets.data.municipalities.map(city => [city, city])} /> : <TextFilter {...filters} name="city" label="Município / cidade" placeholder={params.get('uf') ? 'Nome do município' : 'Escolha uma UF ou digite o nome'} />}
          <TextFilter {...filters} name="manufacturer" label="Marca" />
          <TextFilter {...filters} name="model" label="Modelo" />
          <TextFilter {...filters} name="manufacture_year" label="Ano de fabricação" placeholder="Ex.: 2020" />
          <SelectFilter {...filters} name="vehicle_type" label="Tipo do veículo" options={(facets.data?.vehicle_types || []).map(value => [value, vehicleType(value)])} />
          <SelectFilter {...filters} name="powertrain_type" label="Eletrificação" options={(facets.data?.powertrains || []).map(value => [value, electrification(value)])} />
          {!!activeFilters.length && <div className="active-filter-list" aria-label="Filtros ativos">{activeFilters.map(([key, label]) => <button key={key} onClick={() => update(key, '')}>{label}<X size={12} /></button>)}</div>}
        </div>
      </div>
    </section>
    {result.error ? <ErrorState error={result.error} retry={result.reload} /> : result.loading ? <Loading /> : result.data && <>
      <div className="section-heading"><div><h2>{month(result.data.publication?.reference_period)}</h2><p>{number(result.data.total)} registros agregados{result.data.total_quantity != null ? ` · ${number(result.data.total_quantity)} veículos publicados no recorte` : ''}</p></div><Badge><Database size={13} />SENATRAN</Badge></div>
      {result.data.coverage && <Note>{result.data.coverage}</Note>}
      <div className="table-container"><table><thead><tr><th>Marca e modelo</th><th>Ano</th><th>Município / UF</th><th>Tipo do veículo</th><th>Eletrificação</th><th className="numeric">Quantidade</th><th><span className="sr-only">Acesso ao modelo</span></th></tr></thead><tbody>{result.data.items.map((row, index) => <tr key={row.id || index}><td><strong>{row.manufacturer || 'Marca pendente'}</strong><small>{row.model}{row.trim ? ` · ${row.trim}` : ''}</small></td><td>{row.manufacture_year ?? 'Não informado'}</td><td>{row.municipality || 'Não informado'}<small>{row.state || 'UF não informada'}</small></td><td>{vehicleType(row.vehicle_category)}</td><td>{row.powertrain_type ? electrification(row.powertrain_type) : 'Pendente'}</td><td className="numeric"><strong>{number(row.quantity)}</strong></td><td>{row.model_id && <Link className="text-link" to={`/veiculos/modelos/${row.model_id}?publication_id=${result.data?.publication?.id || ''}`} title="Ver cadastros nacionais deste modelo">Ver cadastros<ArrowUpRight size={15} /></Link>}</td></tr>)}</tbody></table></div>
      {!result.data.items.length && <Empty title="Nenhum registro nesse recorte">Mude a competência, o município ou os filtros de modelo.</Empty>}
      <Pager total={result.data.total} offset={offset} setOffset={setOffset} />
    </>}
    {!!heavy.data?.items.length && <section className="panel">
      <div className="section-heading"><div><h2>Composição dos veículos pesados</h2><p>{number(heavy.data.total_quantity || 0)} registros publicados no arquivo estatístico de tipo, espécie e eixos.</p></div><Badge><Database size={13} />SENATRAN 08</Badge></div>
      <Note>{heavy.data.notice}</Note>
      <div className="table-container"><table><thead><tr><th>Tipo</th><th>Espécie</th><th>Município / UF</th><th>Eixos</th><th className="numeric">Quantidade</th></tr></thead><tbody>{heavy.data.items.map(row => <tr key={row.id}><td>{vehicleType(row.vehicle_type)}</td><td>{row.species || 'Não informada'}</td><td>{row.municipality || 'Não informado'}<small>{row.state || 'UF não informada'}</small></td><td>{row.axles ?? 'Não informado'}</td><td className="numeric"><strong>{number(row.quantity)}</strong></td></tr>)}</tbody></table></div>
    </section>}
    <section className="panel"><h2>Histórico disponível</h2><p>Cada competência é uma fotografia independente. As quantidades de meses diferentes não são somadas.</p>{pubs.error ? <AlertInline text={pubs.error} /> : <div className="period-list">{pubs.data?.items.map(pub => <button key={pub.id} className={'period-button ' + (params.get('publication_id') === pub.id ? 'selected' : '')} onClick={() => update('publication_id', pub.id)}><span>{month(pub.reference_period)}</span><small>{pub.status === 'complete' ? 'Publicada' : pub.status || 'Disponível'}</small></button>)}{pubs.data?.missing_references?.map(reference => <button key={reference} className="period-button missing" disabled><span>{month(reference)}</span><small>Aguardando SENATRAN</small></button>)}</div>}</section>
  </>;
}
function AlertInline({ text }: { text: string }) { return <p role="alert">{text}</p>; }
