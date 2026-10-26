import { useEffect, useState, type ReactNode } from 'react';
import { Link, NavLink, Outlet, useLocation, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Battery, Car, ChevronRight, CircleAlert, Database, Info, LoaderCircle, LogOut, Settings, ShieldCheck } from 'lucide-react';
import { useAuth } from './auth';

export const number = (value: number | undefined) => value == null ? '—' : new Intl.NumberFormat('pt-BR').format(value);
export const date = (value: unknown) => { if (typeof value !== 'string' && typeof value !== 'number') return 'Não informada'; if (!value) return 'Não informada'; const parsed = new Date(String(value)); return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('pt-BR'); };
export const month = (value?: string) => value ? new Date(value.slice(0, 7) + '-01T12:00:00').toLocaleDateString('pt-BR', { month: 'long', year: 'numeric' }) : 'Sem publicação';
const VEHICLE_TYPE_LABELS: Record<string, string> = {
  light_commercial: 'Utilitário', utility: 'Utilitário', other: 'Outro tipo',
  car: 'Automóvel', passenger_car: 'Automóvel', motorcycle: 'Moto', bus: 'Ônibus',
  microbus: 'Micro-ônibus', truck: 'Caminhão', tractor_truck: 'Caminhão-trator',
  chassis_platform: 'Chassi-plataforma', van: 'Van', unknown: 'Tipo pendente',
  // Estes valores antigos descrevem porte ou uma classe regulatória, não o tipo.
  light: 'Tipo pendente', heavy: 'Tipo pendente', LMT: 'Tipo pendente',
};
export const vehicleType = (value?: string) => VEHICLE_TYPE_LABELS[value || ''] || value || 'Tipo pendente';
export const vehicleTypeOptions: [string, string][] = [
  'car', 'motorcycle', 'bus', 'microbus', 'truck', 'tractor_truck', 'van',
  'utility', 'chassis_platform', 'other', 'unknown',
].map(value => [value, vehicleType(value)]);
// Compatibilidade temporária para componentes antigos fora do fluxo principal.
export const category = vehicleType;
export const electrification = (value: string) => ({ MHEV: 'Híbrido leve', BEV: 'Elétrico', HEV: 'Híbrido', HYBRID: 'Híbrido · tipo não informado', PHEV: 'Híbrido plug-in', HEV_FLEX: 'Híbrido flex', MHEV_12V: 'Híbrido leve 12 V', MHEV_48V: 'Híbrido leve 48 V', FCEV: 'Célula de combustível' }[value] || value);
export function Shell() {
  const { user, logout } = useAuth(); const location = useLocation();
  useEffect(() => { window.scrollTo(0, 0); }, [location.pathname]);
  return <><a className="skip" href="#conteudo">Ir para o conteúdo</a><header className="topbar"><div className="topbar-inner"><Link className="brand" to="/veiculos" aria-label="NEXUS — início">NEXUS</Link><div className="brand-caption">VEÍCULOS<br /><span>& BATERIAS</span></div><nav aria-label="Menu principal"><NavLink to="/veiculos"><Car size={19} />Veículos</NavLink><NavLink to="/relatorios"><Database size={19} />Relatórios</NavLink><NavLink to="/oportunidades"><Battery size={19} />Oportunidades</NavLink><NavLink to="/admin"><Settings size={19} />Administração</NavLink></nav><div className="session">{user ? <><span><ShieldCheck size={15} />{user}</span><button className="icon-button" title="Sair" aria-label="Sair da conta" onClick={() => void logout()}><LogOut size={18} /></button></> : <span>Consulta pública</span>}</div></div></header><main id="conteudo" className="main"><Outlet /></main><footer className="site-footer"><span><b>NEXUS</b> Veículos e baterias</span><span>Estatísticas com origem. Cadastros com histórico.</span></footer></>;
}
export function Crumbs({ items }: { items: { label: string; to?: string }[] }) { return <nav className="crumbs" aria-label="Caminho da página"><Link to="/veiculos">Veículos</Link>{items.map((item, i) => <span key={i}><ChevronRight size={14} />{item.to ? <Link to={item.to}>{item.label}</Link> : <span aria-current="page">{item.label}</span>}</span>)}</nav>; }
export function Heading({ eyebrow, title, description, children }: { eyebrow: string; title: string; description?: string; children?: ReactNode }) { return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1>{description && <p>{description}</p>}</div>{children}</div>; }
export function Note({ children, warning = false }: { children: ReactNode; warning?: boolean }) { return <div className={'note' + (warning ? ' warning' : '')}>{warning ? <CircleAlert size={19} /> : <Info size={19} />}<div>{children}</div></div>; }
export function Alert({ error, success }: { error?: string; success?: string }) { return error ? <div className="message error" role="alert"><CircleAlert size={18} />{error}</div> : success ? <output className="message success"><ShieldCheck size={18} />{success}</output> : null; }
export function Loading() { return <output className="empty"><LoaderCircle className="spin" size={24} /><span>Carregando informações…</span></output>; }
export function Empty({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><Battery size={28} /><h3>{title}</h3>{children && <p>{children}</p>}</div>; }
export function ErrorState({ error, retry }: { error: string; retry?: () => void }) { return <div className="empty error-state" role="alert"><CircleAlert /><h3>Não foi possível carregar</h3><p>{error}</p>{retry && <button className="button secondary" onClick={retry}>Tentar novamente</button>}</div>; }
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={'badge ' + tone}>{children}</span>; }
export function Stat({ value, label }: { value: number | undefined; label: string }) { return <div className="stat"><strong>{number(value)}</strong><span>{label}</span></div>; }
export function Pager({ total, offset, limit = 50, setOffset }: { total: number; offset: number; limit?: number; setOffset: (n: number) => void }) { return <div className="pager"><span>{total ? `${number(offset + 1)}–${number(Math.min(offset + limit, total))} de ${number(total)}` : 'Nenhum resultado'}</span><div><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}><ArrowLeft size={16} />Anterior</button><button className="button secondary" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>Próxima<ArrowRight size={16} /></button></div></div>; }
export function useFilters() {
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    const legacyType = params.get('vehicle_category');
    if (legacyType && !params.has('vehicle_type')) setParams(current => {
      const next = new URLSearchParams(current);
      next.set('vehicle_type', legacyType);
      next.delete('vehicle_category');
      return next;
    }, { replace: true });
  }, [params, setParams]);
  const update = (name: string, value: string) => setParams(current => { const next = new URLSearchParams(current); if (value) next.set(name, value); else next.delete(name); if (name !== 'offset') next.delete('offset'); return next; }, { replace: true });
  const offset = Math.max(0, Number(params.get('offset')) || 0);
  return { params, update, offset, setOffset: (value: number) => update('offset', String(value)), clear: () => setParams({}) };
}
export function TextFilter({ name, label, params, update, placeholder }: { name: string; label: string; params: URLSearchParams; update: (name: string, value: string) => void; placeholder?: string }) {
  const source = params.get(name) || '';
  const [value, setValue] = useState(source);
  const [previousSource, setPreviousSource] = useState(source);
  if (source !== previousSource) { setPreviousSource(source); setValue(source); }
  return <label className="field"><span>{label}</span><input name={name} value={value} placeholder={placeholder || 'Todos'} onChange={e => setValue(e.target.value)} onBlur={() => { if (value !== (params.get(name) || '')) update(name, value); }} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); update(name, value); } }} /></label>;
}
export function SelectFilter({ name, label, params, update, options }: { name: string; label: string; params: URLSearchParams; update: (name: string, value: string) => void; options: [string, string][] }) { return <label className="field"><span>{label}</span><select value={params.get(name) || ''} onChange={e => update(name, e.target.value)}><option value="">Todos</option>{options.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>; }
export function AdminHint() { const location = useLocation(); return <p className="admin-hint"><ShieldCheck size={16} />Consulta pública. <Link to={'/admin?returnTo=' + encodeURIComponent(location.pathname + location.search)}>Entrar para editar</Link></p>; }
