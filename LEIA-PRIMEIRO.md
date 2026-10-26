# Nexus no servidor Servidori9

Esta pasta é uma cópia isolada para **Linux Mint 22.3 x86_64**. O projeto original no Git permanece intacto. O Portainer continua na porta 8000; o Nexus usa a **8001** no IP local `10.48.155.129` e, se configurado, no IP do ZeroTier.

## Ordem dos arquivos

1. **01-VERIFICAR-SERVIDOR.sh** — mostra requisitos e portas, sem instalar nada.
2. **02-INSTALAR-NEXUS.sh** — instala e configura a aplicação, banco, conta administrativa, serviço de início automático e backup diário.
3. **03-ATIVAR-ZEROTIER.sh** — após criar uma rede privada no painel ZeroTier, entra nela. Autorize o computador no painel e abra este arquivo de novo para publicar o Nexus no IP virtual.
4. **04-TESTAR-INICIALIZACAO.sh** — confere serviço, contêineres, política de reinício e saúde da API. Execute uma vez após instalar e outra após reiniciar o computador em horário autorizado. O arquivo **não reinicia** o servidor.
5. **05-ABRIR-NEXUS.sh** — abre o site na rede local.
6. **06-BACKUP-NEXUS.sh** — cria um backup manual em `/var/backups/nexus`.
7. **07-INICIAR-NEXUS.sh**, **08-REINICIAR-NEXUS.sh** e **09-PARAR-NEXUS.sh** — controlam a aplicação.
8. **10-CONFIGURAR-ADMIN.sh** — cria a conta se a etapa 02 for interrompida antes disso. Uma senha existente não é redefinida por esse arquivo.

No Gerenciador de Arquivos do Mint, abra o arquivo e escolha **Executar no terminal**. Se o Mint mostrar o conteúdo em vez de executar, marque o arquivo como executável em *Propriedades → Permissões* e tente novamente. Extraia a pasta no diretório pessoal do usuário `servidor`, em um caminho sem espaços.

## O que é automático

O servidor informado já tem Docker Engine e Docker Compose. A etapa 02 preserva essa instalação e o Portainer. O Docker baixa as imagens, e a compilação baixa as dependências Python e Node. O instalador gera segredos aleatórios, aplica migrações, verifica `/health`, pede uma senha **nova** para o administrador e configura systemd e backup diário.

O ZeroTier é opcional durante a etapa 02. A autorização de cada computador exige ação no painel ZeroTier; nenhum script consegue aprovar isso sem credenciais administrativas da rede. A porta 8001 deve ser liberada pela TI para os usuários apropriados. Confirme também que `10.48.155.129` é IP fixo.

## Antes do uso regular

Os backups iniciais ficam no mesmo disco do servidor; copie-os para um destino externo aprovado pela TI. O acesso LAN e ZeroTier usa HTTP no navegador, portanto configure HTTPS antes de uso regular com contas e senhas. O instalador habilita downloads **manuais** das fontes, mas mantém o agendador de atualizações desligado.

Não é possível garantir ausência de erros no servidor sem executá-lo lá. Falhas de proxy, firewall, acesso às fontes e memória disponível ainda dependem do ambiente real. O script para com mensagem clara se algum pré-requisito não estiver pronto. Não envie `.env`, senhas ou chaves em mensagens. Troque a senha que foi divulgada anteriormente; ela não está nesta pasta.
