# Classificação automática da frota

O arquivo de marca/modelo da SENATRAN informa o nome registrado, ano de fabricação, município e quantidade. Ele não identifica uma bateria individual nem traz a saúde da bateria. A coluna de combustível está em outro relatório agregado e não pode ser vinculada com segurança a cada nome da frota.

Na importação, o NEXUS inclui nomes já cobertos pelo catálogo e nomes cuja versão registrada contém uma indicação explícita de propulsão: BEV, PHEV, HEV, MHEV, híbrido ou elétrico. As siglas curtas `EV` e `DM` só valem para fabricantes permitidos na regra, pois também aparecem em códigos sem relação com propulsão. Um híbrido escrito apenas como “HYBRID” ou “HIBRIDO” recebe `HYBRID` (subtipo não informado), sem ser forçado para HEV ou MHEV. O tipo do veículo permanece desconhecido quando a fonte não o informa.

As linhas aceitas mantêm o ano, município e quantidade. Os nomes sem evidência suficiente ficam fora dos totais; apenas seus nomes são mantidos para diagnóstico, sem trabalho de revisão para o usuário. Cada classificação conserva referência ao arquivo SENATRAN e à versão da regra. Uma nova versão da regra agenda automaticamente a reimportação dos meses publicados quando `ALLOW_SOURCE_DOWNLOAD=true`; a publicação antiga fica ativa até a nova estar completa.

Na instalação verificada em 22/10/2026, a reimportação de setembro de 2026 passou de 514.645 para 753.315 veículos no recorte eletrificado. Dos 187 nomes antes exibidos para revisão, 165 passaram a ser classificados automaticamente e 22 permaneceram fora por ambiguidade.

A aba Oportunidades usa esses novos cadastros no modo fictício. No modo real, um veículo só vira candidato individual depois de receber identificação de bateria e medição de saúde.

Fontes: [glossário da frota SENATRAN](https://www.gov.br/transportes/pt-br/assuntos/transito/arquivos-senatran/estatisticas/renavam/glossario-da-frota-de-veiculos-estatisticas-denatran/) e [tabelas PBE Veicular do Inmetro](https://www.gov.br/inmetro/pt-br/assuntos/regulamentacao/avaliacao-da-conformidade/programa-brasileiro-de-etiquetagem/tabelas-de-eficiencia-energetica/veiculos-automotivos-pbe-veicular/).
