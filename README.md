# NEXUS — Veículos e baterias

O NEXUS organiza os dados mensais da SENATRAN no fluxo **Veículos → modelo → cadastro individual → ficha da bateria**. Relatórios e Administração usam a mesma estrutura visual.

## Instalação no servidor Linux

Para o servidor Linux Mint 22.3 da empresa, consulte [LEIA-PRIMEIRO.md](LEIA-PRIMEIRO.md). Os arquivos `01-VERIFICAR-SERVIDOR.sh` a `10-CONFIGURAR-ADMIN.sh` conduzem a verificação, instalação, acesso por ZeroTier, teste de inicialização, backup e operação. O instalador usa a porta 8001 e preserva o Portainer na porta 8000. Docker Engine e Compose precisam estar funcionando antes da instalação.

As configurações e credenciais ficam no `.env` local, que não entra no Git. A pasta `linux/` contém os scripts individuais de instalação, configuração, atualização, diagnóstico, restauração e remoção. Para atualizar uma instalação existente, execute `git pull --ff-only` e depois `sudo bash linux/update.sh --backup-dir /var/backups/nexus --confirm` na raiz do projeto.

## Instalação recomendada no Windows

1. Baixe e extraia o projeto (ou clone o repositório).
2. Abra **instalar.bat** na raiz ou na pasta **atalhos**. Ele verifica os requisitos, instala Docker pelo WinGet quando possível, gera configurações, prepara PostgreSQL e a aplicação e solicita a criação do administrador.
3. Nos próximos usos, abra **iniciar.bat**. Se Docker ou `.env` estiverem ausentes, esse atalho entra automaticamente no modo de primeira instalação. O navegador abre em http://localhost:8000/veiculos após a aplicação ficar pronta.
4. Pressione **Enter na janela de inicialização** ou use **desligar.bat** para parar os serviços do projeto, preservando os dados.

São necessários Windows compatível com Docker Desktop, virtualização habilitada, WSL 2, internet na instalação e pelo menos 15 GB livres inicialmente. O instalador orienta sobre permissões e reinicialização; não reinicia o computador. Depois de resolver um requisito, execute-o novamente. Dependendo do volume de dados, será necessário mais espaço.

O instalador preserva configurações e contas existentes. Valores de exemplo são identificados. Logs operacionais ficam em `data/logs/windows.log`; senhas não são registradas. A inicialização diária reutiliza as imagens prontas. Se alguma imagem estiver ausente após uma instalação incompleta, iniciar.bat prepara as dependências automaticamente e solicita a criação do administrador caso necessário.

### Dados da Senatran pelo dashboard

Entre em **Administração → Atualizações → Dados da Senatran**. O catálogo é consultado uma vez ao iniciar o programa. Use **Verificar atualizações** para consultar novamente (cliques próximos são agrupados). Não há verificação por hora ou diária.

O NEXUS consulta o catálogo de arquivos e a página oficial de publicações no gov.br. Se o catálogo de marca/modelo estiver indisponível, os meses publicados continuam visíveis pela página oficial, com um link para conferir a fonte. Publicação e disponibilidade do arquivo são indicadas separadamente; o sistema não tenta importar uma página HTML como relatório. Se ambas as fontes falharem, a última lista válida é preservada. Consultas não são repetidas em segundo plano; tente novamente pelo botão depois de pelo menos um minuto. Não é necessário reinstalar.

Escolha o ano e clique em **Baixar e importar mês** no mês desejado. O NEXUS baixa e processa o arquivo de marca/modelo e o relatório de combustível quando disponível. Os demais relatórios não permitem isolar com segurança a frota eletrificada.

Em **Veículos**, escolha o mês no campo **Referência SENATRAN** para consultar os modelos eletrificados reconhecidos. Em **Relatórios**, explore as quantidades por modelo, estado e município. O sistema guarda o recorte eletrificado e a referência da fonte. **Retomar** aproveita os checkpoints existentes. **Verificar revisão** consulta novamente o conteúdo de um mês instalado; só é indicada uma revisão disponível quando os metadados da fonte demonstram mudança.

Os controles são independentes:

```dotenv
I9_CATALOG_AUTO_REFRESH=false
I9_ON_DEMAND_WORKER=true
I9_CHECK_ON_START=true
ALLOW_SOURCE_DOWNLOAD=true
I9_UPDATES_PAUSED=true
```

A instalação padrão usa processamento sob demanda dentro da API: a tarefa termina quando não há mais trabalho, sem outro processo Python permanentemente ocioso. Python e PostgreSQL ficam no Docker; não é necessário instalar Python no Windows. O Compose desativa reinício automático dos serviços e mantém o trabalhador separado apenas no perfil opcional `maintenance`. Os antigos agendamentos deste projeto no Windows são desativados pelo iniciador. Credenciais e dados existentes são preservados.

A consulta do catálogo nunca baixa relatórios. Enquanto você usa o sistema, a API e o banco precisam continuar ativos. Fechar apenas a aba não encerra o sistema; use Enter na janela de inicialização ou desligar.bat. O Docker Desktop pode continuar aberto para outras aplicações.

A alternativa por terminal usa o mesmo serviço do painel:

```powershell
docker compose exec api python -m scripts.download_month --reference 2026-08
```

Acrescente `--refresh` para verificar uma revisão. O clone não inclui frota, banco ou credenciais. Cada mês pode ocupar vários GB durante a importação; os arquivos completos são removidos após a publicação validada. O histórico de publicações e a publicação anterior permanecem disponíveis em caso de falha. Importar um mês antigo não altera a publicação padrão mais recente.

### Garantia e saúde das baterias

Na ficha de uma bateria física, use **Editar garantia** para informar datas, duração em meses, limite/base de quilometragem, instalação, condições e evidência. Referências do modelo são sugestões para conferência; não são aplicadas automaticamente. Os 71 campos do passaporte permanecem independentes.

Em **Registrar evento**, cadastre saúde, ciclos e, quando disponível, hodômetro vinculado à instalação. Leituras de veículos diferentes não são comparadas como um único contador. Após uma troca de veículo, a garantia precisa ter sua aplicação conferida antes de avaliar a quilometragem da nova instalação.

Administração mostra prazos que terminam em até 90 dias, prazos encerrados e limites técnicos atingidos. Os cálculos exibem a data da medição e os dados ausentes. Encerramento de garantia não muda o estado operacional da bateria e não prevê sua data de substituição.

## Desenvolvimento sem Docker no Windows

Para desenvolvimento, com Python 3.14 e Node.js 22 instalados:

```powershell
Copy-Item .env.example .env
pwsh -File scripts/start.ps1 -Install
```

Acesse http://localhost:3000/veiculos. Sem `DATABASE_URL`, essa forma usa SQLite e serve apenas para desenvolvimento e prévia. Para importações mensais reais, prefira PostgreSQL pelo Docker.

Depois da instalação inicial, inicie com:

```powershell
pwsh -File scripts/start.ps1
```

Para baixar uma competência nessa instalação, habilite `ALLOW_SOURCE_DOWNLOAD=true` no `.env` e execute:

```powershell
.venv/Scripts/python.exe -m scripts.download_month --reference 2026-08
```

## Administrador

Leituras são públicas. Alterações exigem sessão administrativa e proteção CSRF. Não existe senha padrão; a senha deve ter pelo menos 12 caracteres. Na instalação recomendada, o instalador solicita a criação da conta. Para redefini-la com o sistema iniciado:

```powershell
docker compose exec api python -m scripts.setup_admin --username admin --reset
```

Substitua `admin` pelo usuário escolhido na instalação.

Para criar a conta em uma instalação local e escolher a senha quando solicitado:

```powershell
.venv/Scripts/python.exe -m scripts.setup_admin --username admin
```

Para recuperar a conta, use o mesmo comando com `--reset`. A troca invalida as sessões existentes. Depois entre em `/admin`.

## Como interpretar os registros

- **Publicados no mês:** estatística de uma única competência da SENATRAN.
- **Cadastros existentes:** fichas permanentes com ID interno. A quantidade pode superar a estatística de um mês posterior.
- **Veículos identificados:** fichas que receberam identificação individual informada ou verificada.
- **Com bateria vinculada:** veículos com registro de pack físico.

Os meses não são somados. A sequência 90 → 95 → 85 → 92 mantém 95 fichas, sem alegar quais carros saíram ou voltaram. Cada ficha abre por URL própria e mantém seu ID.

Uma ficha estatística não prova a identidade de um carro. Placa, chassi, RENAVAM, localização individual e serial permanecem vazios enquanto desconhecidos. Os filtros de estado e município dos relatórios representam a estatística, não a localização dos cadastros pendentes.

A ficha preliminar contém os 71 campos do cliente, com estados pendente, rascunho, confirmado e não aplicável. Ela não cria uma bateria física. Packs são registrados quando existe evidência individual, com vínculos e eventos separados.

## Importação SENATRAN

O importador lê o original em blocos de até 100 mil linhas, filtra as famílias eletrificadas reconhecidas e guarda somente essas observações detalhadas no PostgreSQL. Nomes não reconhecidos são resumidos para revisão. O original compactado é preservado com hash; aprovar uma nova classificação permite reler o arquivo local.

A cobertura é parcial e explícita: nomes que não permitem distinguir combustão de híbrido não são classificados por suposição. Categoria desconhecida não vira automaticamente automóvel.

Na instalação padrão, o processamento é iniciado sob demanda dentro da API. O trabalhador separado é uma opção de manutenção. Fila, tentativas, checkpoints e reservas ficam no PostgreSQL. A nova publicação só entra no ar depois de terminar a preparação e validar as contagens. Downloads remotos habilitados usam trechos verificáveis e retomáveis.

Para processar um arquivo já disponível, sem internet:

```powershell
.venv/Scripts/python.exe -m src.worker --once --no-schedule --import-file CAMINHO_DO_ZIP --reference 2026-07
```

A retomada preserva os blocos confirmados. Administração mostra execução, erro e progresso. A reclassificação de um mês já processado baixa novamente o arquivo oficial e confere seu hash.

## Atualização automática

A instalação padrão não utiliza agendamentos nem inicia junto com o Windows. A consulta ocorre ao iniciar o programa ou pelo botão Verificar atualizações. Os dados brutos completos da SENATRAN são temporários; após a publicação validada, permanece o recorte eletrificado e a referência à fonte.

O dashboard **Oportunidades** mostra baterias individuais medidas e, separadamente, estimativas regionais. Em Administração é possível ativar uma simulação de leitura, com 71 campos por bateria fictícia, sem modificar registros reais. A integração futura de medições está documentada em [Integração de baterias](docs/Integracao_Baterias.md).

## Desenvolvimento e validação

O backend usa Python, FastAPI, SQLAlchemy, Alembic e PostgreSQL 17. O frontend usa React, React Router e Vite e é servido pela própria API após o build.

```powershell
.venv/Scripts/python.exe -m pytest -q
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/test_windows.ps1
Set-Location dashboard
npm.cmd ci
npm.cmd run lint
npm.cmd run build
```

Testes de concorrência exigem `I9_TEST_DATABASE_URL` e usam esquemas descartáveis isolados. Não execute ferramentas que apaguem a base operacional. A documentação histórica anterior está em `docs/README-anterior-v3.md`.

## Repositório e atualização

Versione o código, as migrações, os atalhos, `.env.example` e os arquivos de dependências (incluindo `dashboard/package-lock.json`). `.env`, senhas, volumes, arquivos baixados, logs, `node_modules` e `dist` ficam fora do Git. Um clone novo começa sem os dados mensais: instale o sistema e escolha os meses em Administração.

O nome público é NEXUS. Os identificadores técnicos `I9_*` e o banco `i9` continuam por compatibilidade. Em uma instalação existente, preserve a pasta do projeto e o nome do projeto Compose para reutilizar o volume correto. Não use `docker compose down -v` para atualizar: esse comando remove os volumes.

Para atualizar, execute `instalar.bat` para reconstruir as imagens e aplicar as migrações; depois use `iniciar.bat`. Os testes automatizados do instalador simulam requisitos e preservação de configurações; não substituem a validação em um Windows limpo, especialmente WSL, virtualização, permissões e reinicialização.
