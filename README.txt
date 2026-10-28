COLOMBIA BOT - FILA ABERTA E SERVIÇO DE MEDIADORES (CORRIGIDO)

INSTALAÇÃO
1. Extraia o ZIP.
2. Crie um arquivo chamado .env nesta pasta e coloque nele DISCORD_TOKEN=SEU_TOKEN. Não compartilhe o token.
3. Abra o Prompt de Comando nesta pasta e execute: py -m pip install -r requirements.txt
4. Execute: py bot.py

FLUXO DA FILA
- Use /fila para publicar uma fila aberta no canal atual.
- Dois jogadores entram na fila; o bot cria um canal privado para negociar.
- No canal privado, ambos clicam em CONFIRMAR APOSTA.
- O botão FECHAR FILA + TRANSCRIPT permite que um dos jogadores, o mediador responsável ou um administrador encerre a aposta. O bot gera um arquivo TXT com o histórico e um código único, enviando por PV aos participantes; se alguém bloquear mensagens privadas, o envio para essa pessoa falha, mas o código aparece no canal.
- Mediadores com o cargo configurado usam /servico_mediador para entrar/sair de serviço.
- Apenas mediadores em serviço veem apostas sem responsável. O primeiro que clicar em Assumir aposta fica responsável; os demais perdem acesso individual ao canal.
- Chave PIX e QR podem ser configurados; pagamentos precisam ser conferidos manualmente.

CORREÇÃO DESTA VERSÃO
- Corrigido o erro `AttributeError: 'NoneType' object has no attribute 'id'` ao criar o canal: o bot agora resolve os membros e valida os alvos de permissões antes de criar o canal.
- Se algum jogador não estiver mais no servidor ou faltar permissão, o bot responde com uma mensagem de erro útil em vez de falhar silenciosamente.
- Adicionados canais de logs separados por categoria, com registro detalhado também salvo no banco SQLite.


IMAGEM PERSONALIZADA NO EMBED DA FILA
1. Inicie o bot e execute /painel como administrador.
2. Clique em “Imagem da Fila”.
3. Para usar um link, clique em “Colar URL da imagem” e informe uma URL pública começando com https://.
4. Para enviar um arquivo, clique em “Enviar imagem aqui” e anexe a imagem no canal onde o painel foi aberto dentro de 120 segundos. O bot tenta apagar a mensagem com o anexo depois de salvar.
5. Para retirar a imagem, clique em “Remover imagem”.
A imagem aparece como miniatura nos embeds das filas novas e nas filas automáticas. A configuração fica salva em config.json. Para maior durabilidade, prefira uma URL pública estável; links de anexos do Discord podem expirar.

CONFIGURAÇÃO
Para corrigir o cargo de mediador, execute `/configurar_cargo_mediador` e selecione o cargo no menu. O bot confirma o nome e o ID salvos. Se o botão ainda falhar, a mensagem agora informa qual cargo/ID está configurado e o que falta.
Use /configurar para ajustar IDs e valores. Para os logs, configure:
- canal_logs_id: canal geral de logs/fallback
- logs_filas_id: criação e ações de filas
- logs_apostas_id: fallback geral de eventos de apostas
- logs_apostas_abertas_id: filas/apostas abertas e novas filas automáticas
- logs_apostas_fechadas_id: apostas encerradas/canceladas
- logs_apostas_confirmadas_id: apostas confirmadas pelos dois jogadores
- logs_apostas_finalizadas_id: apostas finalizadas pelo mediador
- logs_partidas_id: criação e eventos das partidas
- logs_mediadores_id: serviço e atribuição de mediadores
- logs_blacklist_id: eventos de blacklist
- logs_admin_id: configurações e ações administrativas

Se um canal de log específico não estiver configurado, os eventos usam o canal geral de apostas `logs_apostas_id` e, se ele também não estiver configurado, `canal_logs_id`. Configure cada opção escolhendo o nome correspondente em `/configurar` e colocando no campo `valor` apenas o ID numérico do canal.
Configure o cargo com `/configurar_cargo_mediador` (selecione o cargo diretamente) ou `cargo_mediador_id` e a categoria de canais privados com `categoria_mediadores_id` (ou `categoria_apostas_id`). O bot precisa de permissões para gerenciar canais, ver canais e enviar mensagens.

IMPORTANTE
Não apague nem substitua `colombia_bot.sqlite3` ou `config.json` se quiser preservar os dados anteriores. Faça backup antes de atualizar.


CONFIGURAÇÃO DO CARGO DE MEDIADOR POR SERVIDOR

A versão atual salva o cargo de mediador separadamente para cada servidor do Discord.
1. Inicie o bot e convide-o para o servidor com permissões para usar comandos e gerenciar canais.
2. No servidor desejado, use /configurar_cargo_mediador e selecione o cargo na lista.
3. Publique o painel com /painel_mediadores.
4. Os usuários que tiverem o cargo selecionado nesse servidor podem usar ENTRAR EM FILA e SAIR DA FILA.

Atenção: não reutilize um config.json antigo com uma configuração incorreta sem executar novamente /configurar_cargo_mediador em cada servidor. O comando novo grava o cargo por servidor.

CONFIGURAÇÃO PIX INDIVIDUAL DOS MEDIADORES
- Publique o painel com /painel_mediadores.
- Cada mediador com o cargo configurado pode clicar em CONFIGURAR MEU PIX e informar tipo, chave e, opcionalmente, URL de imagem do QR Code.
- VER MEU PIX mostra os dados somente em resposta privada (ephemeral) ao próprio mediador.
- Os dados são guardados no banco SQLite por servidor e usuário, separados da chave PIX global do administrador.
- Ao assumir uma aposta, o bot usa o PIX individual do mediador responsável, quando cadastrado; se ainda não houver PIX individual, mantém o PIX global configurado como alternativa.
