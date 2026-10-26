import { Link } from 'react-router-dom';
import { useState } from 'react';
import { randomId, useResource, write } from './api';
import { Alert, Badge, date, Loading, month, Note, number } from './ui';

interface Job { id: string; status: string; stage: string; error?: string; progress?: { downloaded_bytes?: number; download_bytes?: number; scanned_rows?: number; materialized_units?: number; planned_units?: number }; }
interface Dataset { name?: string; published?: boolean; publication_url?: string; kind: string; available: boolean; ambiguous: boolean; revision_available: boolean; job?: Job; }
interface Month { reference: string; installed: boolean; datasets: Dataset[]; reports?: Dataset[]; }
const labels: Record<string, string> = { queued: 'Na fila', running: 'Processando', complete: 'Instalado', failed: 'Falha', paused: 'Pausado' };
export function catalogError(message: string) {
  if (message.includes('503')) return 'O portal da Senatran está temporariamente indisponível (HTTP 503). Tente novamente mais tarde.';
  return message;
}
function Progress({ dataset }: { dataset: Dataset }) {
  const job = dataset.job; const p = job?.progress;
  const downloading = job?.status === 'running' && job.stage.includes('download');
  const total = downloading ? p?.download_bytes : p?.planned_units;
  const done = downloading ? p?.downloaded_bytes : p?.materialized_units;
  return <div><strong>{dataset.name || (dataset.kind === 'import_remote' ? 'Marca e modelo' : 'Tipo, espécie e eixos')}</strong>{' '}<Badge>{dataset.ambiguous ? 'Fonte ambígua' : downloading ? 'Baixando' : job ? labels[job.status] || job.status : dataset.available ? 'Disponível' : dataset.published ? 'Publicado · arquivo indisponível' : 'Indisponível'}</Badge>
    {dataset.revision_available && <Badge>Revisão disponível</Badge>}
    {job?.status === 'running' && <small>{job.stage}{total && done != null ? ` · ${Math.min(100, Math.round(done / total * 100))}%` : ''}{p?.scanned_rows != null ? ` · ${number(p.scanned_rows)} linhas lidas` : ''}{downloading && p?.downloaded_bytes != null ? ` · ${(p.downloaded_bytes / 1048576).toFixed(1)} MB baixados` : ''}</small>}
    {job?.error && <Alert error={job.error} />}
  </div>;
}
export function SenatranPanel() {
  const result = useResource<{ items: Month[]; checked_at?: string; attempted_at?: string; refresh_status?: string; source_warning?: string; last_error?: string }>('/admin/senatran/months', 10000);
  const [year, setYear] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [message, setMessage] = useState('');
  async function act(path: string, body: unknown) { setBusy(true); setError(''); setMessage(''); try { const response = await write<{ status?: string }>(path, body); setMessage(path.endsWith('/refresh') ? response.status === 'failed' ? 'A última consulta falhou há menos de um minuto. Aguarde antes de tentar novamente.' : response.status === 'complete' ? 'A lista já foi consultada no último minuto.' : '' : 'Importação solicitada. Acompanhe o progresso abaixo.'); result.reload(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  const checking = ['queued', 'running'].includes(result.data?.refresh_status || '');
  const years = [...new Set(result.data?.items.map(m => m.reference.slice(0, 4)) || [])];
  return <section className="panel"><div className="section-heading"><div><h2>Dados da Senatran</h2><p>Escolha o mês. O NEXUS encontra, baixa e importa todos os relatórios disponíveis automaticamente.</p><small>Última consulta concluída: {result.data?.checked_at ? date(result.data.checked_at) : 'Nenhuma'}</small>{result.data?.attempted_at && <small>Última tentativa: {date(result.data.attempted_at)}</small>}</div><button className="button secondary" disabled={busy || checking} onClick={() => act('/admin/senatran/refresh', {})}>{checking ? 'Consultando Senatran…' : 'Verificar atualizações'}</button></div>
    <Alert error={error || result.error} />{checking && <Note>Consultando os meses publicados. Nenhum relatório será baixado nesta etapa.</Note>}{result.data?.last_error && <Note warning>{catalogError(result.data.last_error)} {result.data.checked_at ? 'A última lista conhecida foi preservada.' : 'Ainda não foi possível obter a primeira lista de meses.'}</Note>}{result.data?.source_warning && <Note warning>{result.data.source_warning}</Note>}{message && <Note>{message}</Note>}
    <label className="field"><span>Ano</span><select value={year} onChange={e => setYear(e.target.value)}><option value="">Todos</option>{years.map(y => <option key={y}>{y}</option>)}</select></label>
    {!result.data ? <Loading /> : !result.data.items.length ? <Note>{result.data.last_error ? 'A seleção de mês ficará disponível quando a consulta à Senatran for concluída. Não é necessário reinstalar o sistema.' : 'Nenhuma competência disponível na lista. Aguarde a consulta ou use Verificar atualizações.'}</Note> : result.data.items.filter(m => !year || m.reference.startsWith(year)).map(m => {
      const datasets = [...m.datasets, ...(m.reports || [])];
      const active = datasets.some(d => d.job && ['queued', 'running'].includes(d.job.status));
      const blocked = !datasets.some(d => d.available) || datasets.some(d => d.ambiguous);
      return <article className="history-row" key={m.reference}><div><h3>{month(m.reference)}</h3>{datasets.map(d => <Progress key={d.kind} dataset={d} />)}{m.datasets[0].publication_url && <a className="text-link" href={m.datasets[0].publication_url} target="_blank" rel="noreferrer">Ver publicação no site da Senatran</a>}{!m.datasets[0].available && <small>Marca e modelo pendente na fonte. Os demais relatórios podem ser importados e consultados em Relatórios.</small>}{m.installed && <small>Publicação disponível para consulta nos relatórios.</small>}</div><div className="actions">
        {datasets.some(d => d.job?.status === 'complete') && <Link className="button secondary" to={`/relatorios?reference=${m.reference}`}>Ver dados de {month(m.reference)}</Link>}
        {datasets.filter(d => d.job && ['failed', 'paused'].includes(d.job.status)).map(d => <button key={d.kind} className="button secondary" disabled={busy} onClick={() => act(`/admin/jobs/${d.job!.id}/retry`, {})}>Retomar {d.name || (d.kind === 'import_remote' ? 'marca/modelo' : 'complemento')}</button>)}
        <button className="button" disabled={busy || active || blocked} onClick={() => act('/admin/senatran/import', { reference: m.reference, refresh: m.installed, idempotency_key: randomId() })}>{active ? 'Em andamento' : blocked ? 'Arquivos indisponíveis' : m.installed ? m.datasets.some(d => d.revision_available) ? 'Atualizar mês' : 'Verificar revisão' : 'Baixar e importar mês'}</button>
      </div></article>;
    })}<Note>Fechar o navegador não interrompe o processamento. O computador e o Docker precisam continuar ligados.</Note>
  </section>;
}
