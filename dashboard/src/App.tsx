import { Navigate, Route, Routes, useSearchParams } from 'react-router-dom';
import { AdminPage } from './admin';
import { BatteryPage, DossierPage } from './batteries';
import { ReportsPage } from './reports';
import { Empty, Shell } from './ui';
import { ModelPage, ModelsPage, VehiclePage } from './vehicles';
import { OpportunitiesPage, SimulatedPassportPage } from './opportunities';

function LegacyIntelligence() { const [params] = useSearchParams(); const area = params.get('area'); return <Navigate replace to={area === 'relatorios' ? '/relatorios' : area === 'dados' || area === 'importar' || area === 'admin' || area === 'acesso' ? '/admin' : '/veiculos'} />; }
export default function App() { return <Routes><Route element={<Shell />}><Route index element={<Navigate replace to="/veiculos" />} /><Route path="/veiculos" element={<ModelsPage />} /><Route path="/veiculos/modelos/:id" element={<ModelPage />} /><Route path="/veiculos/unidades/:id" element={<VehiclePage />} /><Route path="/veiculos/unidades/:id/ficha-bateria" element={<DossierPage />} /><Route path="/baterias/:id" element={<BatteryPage />} /><Route path="/relatorios" element={<ReportsPage />} /><Route path="/oportunidades" element={<OpportunitiesPage />} /><Route path="/oportunidades/simulacao/:id" element={<SimulatedPassportPage />} /><Route path="/admin" element={<AdminPage />} /><Route path="/inteligencia" element={<LegacyIntelligence />} /><Route path="/frota" element={<Navigate replace to="/relatorios" />} /><Route path="*" element={<Empty title="Página não encontrada">Use o menu para voltar aos veículos, relatórios ou administração.</Empty>} /></Route></Routes>; }
