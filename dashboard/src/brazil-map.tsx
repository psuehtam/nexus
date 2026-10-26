/* SVG paths cannot be HTML buttons; roles and keyboard handlers expose the interactive map. */
/* oxlint-disable jsx-a11y/prefer-tag-over-role */
import brazil from '@svg-maps/brazil';

interface BrazilMapProps {
  selected?: string;
  available?: string[];
  quantities?: Record<string, number>;
  onSelect: (uf: string) => void;
}

export function BrazilMap({ selected, available = [], quantities = {}, onSelect }: BrazilMapProps) {
  const availableSet = new Set(available.map(value => value.toUpperCase()));
  const locations = brazil.locations as Array<{ id: string; name: string; path: string }>;
  const maximum = Math.max(0, ...Object.values(quantities));
  const level = (quantity: number) => maximum && quantity ? Math.max(1, Math.min(5, Math.ceil((Math.log1p(quantity) / Math.log1p(maximum)) * 5))) : 0;
  const activate = (uf: string) => { if (availableSet.has(uf)) onSelect(uf); };
  return <div className="brazil-map-wrap">
    <svg className="brazil-map" viewBox={brazil.viewBox} role="group" aria-label="Mapa do Brasil para selecionar um estado">
      {locations.map(location => {
        const uf = location.id.toUpperCase(); const enabled = availableSet.has(uf); const active = selected === uf; const quantity = quantities[uf] || 0;
        return <path key={uf} d={location.path} className={`map-state level-${level(quantity)}${active ? ' selected' : ''}${enabled ? '' : ' unavailable'}`}
          role="button" tabIndex={enabled ? 0 : -1} aria-label={`${location.name}: ${quantity.toLocaleString('pt-BR')} veículos${active ? ', selecionado' : ''}`}
          aria-pressed={active} onClick={() => activate(uf)}
          onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(uf); } }}>
          <title>{location.name} ({uf}) · {quantity.toLocaleString('pt-BR')} veículos</title>
        </path>;
      })}
    </svg>
    <div className="map-scale" aria-label="Escala de quantidade de veículos"><span>Menos</span>{[1, 2, 3, 4, 5].map(value => <i key={value} className={`level-${value}`} />)}<span>Mais veículos</span></div>
    <div className="map-caption"><span><i className="selected" />Estado selecionado</span></div>
    <a className="map-credit" href="https://github.com/VictorCazanave/svg-maps/tree/master/packages/brazil" target="_blank" rel="noreferrer">Mapa CC BY 4.0 · svg-maps</a>
  </div>;
}
