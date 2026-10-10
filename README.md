# LudoTrack — um "Last.fm para jogos"

Bot de Telegram que mostra o que você está jogando, a partir da sua **atividade no
Discord**. Qualquer pessoa pode se cadastrar (adicionando o bot do Discord ao próprio
servidor) e usar `/nowplaying` no privado ou num grupo para mostrar aos amigos o jogo
atual — na hora, com capa e os detalhes que o jogo publica no Discord (modo, mapa,
placar/KDA, personagem, elo, grupo) — e `/stats` para ver os jogos mais jogados da
semana e do mês.

Funciona com qualquer jogo que o Discord detecte sozinho (League of Legends,
Valorant, TFT, jogos da Steam/Epic etc.), sem API específica por jogo.

## Como funciona

```
Discord (presence) ──► discord_presence.py ──► main.py (GameTracker) ──► db.py (SQLite) ──► supabase_sync.py ──► Supabase
   /vincular ─────────► accounts.py ◄────────── telegram_bot.py (/register)        ▲         (background, opcional)
                                                telegram_bot.py (/nowplaying, /stats) ──┘ + igdb_client.py
```

- O bot do Discord pode estar em **qualquer número de servidores**: cada pessoa o
  adiciona ao próprio servidor pelo botão **Adicionar ao Discord** do `/register`.
  Ele escuta `on_presence_update` de quem se cadastrou, em qualquer servidor em comum.
- **Cadastro sem senha:** `/register` no Telegram gera um código; a pessoa digita
  `/vincular CÓDIGO` num servidor com o bot. Só o dono da conta do Discord consegue
  fazer isso, então o vínculo é seguro sem pedir token ou senha.
- Só activities do tipo `playing` contam (Spotify, status personalizado etc. são ignorados).
- **Detecção imediata:** abrir ou trocar de jogo vale na hora, e o `/nowplaying` lê a
  presence ao vivo. Só o **fechamento** tem tolerância de 15s: se o jogo sumir e voltar
  nesse intervalo (oscilação da presence), a sessão continua; se não voltar, ela termina
  no momento em que o jogo sumiu.
- **Capa e detalhes:** a capa vem da IGDB; sem ela, usa a imagem do Rich Presence do jogo.
  Os detalhes extras (modo, mapa, placar, KDA, farm, personagem, elo, tamanho do grupo)
  são os que **o próprio jogo publica no Discord** — cada jogo mostra coisas diferentes,
  e muitos não publicam nada além do nome. O League of Legends, por exemplo, publica
  modo/mapa e campeão, mas não KDA nem farm.
- O bot **não manda avisos automáticos**: cada pessoa decide quando mostrar o que está
  jogando, com `/nowplaying`.
- Discord e Telegram rodam no **mesmo event loop asyncio** (`asyncio.TaskGroup`).
- Falhas na IGDB só geram log; a resposta sai sem capa.
- Se o processo cair no meio de um jogo, a sessão é fechada no último *heartbeat*
  (gravado a cada 60s) ao reiniciar. Se reiniciar em menos de 5 min, a sessão é retomada.

## Pré-requisitos

- Windows com PowerShell
- Python 3.11+ (`python --version`)

## 1. Instalação

```powershell
cd "C:\caminho\para\botjogos"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

> Se o `Activate.ps1` for bloqueado ("execução de scripts foi desabilitada"), rode uma vez:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

## 2. Criar o bot no Discord

1. Acesse <https://discord.com/developers/applications> e clique em **New Application**.
2. No menu lateral, abra **Bot**:
   - Clique em **Reset Token** e copie o token para `DISCORD_BOT_TOKEN` no `.env`.
   - Em **Privileged Gateway Intents**, ligue:
     - ✅ **PRESENCE INTENT** (obrigatório: é o que permite ver o que as pessoas estão jogando)
     - ✅ **SERVER MEMBERS INTENT** (para o bot encontrar os usuários na lista de membros)
   - Clique em **Save Changes**.

   Sem esses intents o bot para com o erro `PrivilegedIntentsRequired`.
3. Ainda em **Bot**, deixe **Public Bot** ligado, para qualquer pessoa poder adicioná-lo
   ao próprio servidor.

### Os servidores do Discord

O Discord só envia a presence de quem compartilha um servidor com o bot. Não é preciso
configurar servidor nenhum: ao rodar, o bot loga `Link para adicionar o bot a um servidor: ...`,
e o mesmo link aparece no botão **Adicionar ao Discord** do `/register`. Cada pessoa
adiciona o bot a um servidor seu (pode ser um servidor vazio, criado só para isso) e
usa `/vincular` lá. O link já pede os scopes `bot` e `applications.commands`, sem permissões.

(Opcional) Se quiser um servidor "oficial" para quem não tem servidor próprio, crie um
convite sem expiração e coloque em `DISCORD_INVITE_URL`: ele vira o botão **Entrar no servidor**.

Até 100 servidores o bot não precisa de verificação do Discord para usar o Presence Intent;
acima disso o Discord exige verificar o bot e justificar o uso do intent.

### A atividade precisa estar visível

Cada usuário precisa deixar ligado **Configurações > Privacidade de atividade >
"Compartilhar sua atividade detectada com outras pessoas"**. Quem estiver
*invisível* não tem a atividade enviada para ninguém, nem para o bot.

### Testar só o Discord

Dá para rodar só a detecção, passando os IDs de usuário a acompanhar
(botão direito no nome > **Copiar ID do usuário**):

```powershell
python discord_presence.py 123456789012345678
```

Abra um jogo e veja o log `SESSÃO INICIADA` na hora; feche e, após 15s, `SESSÃO ENCERRADA`.
Para ver cada presence update cru, use `$env:LOG_LEVEL = "DEBUG"` antes de rodar.

## 3. Criar o bot no Telegram

Fale com o [@BotFather](https://t.me/BotFather), envie `/newbot` e copie o token para
`TELEGRAM_BOT_TOKEN`. Não precisa configurar chat: o bot responde no privado e em
qualquer grupo em que for adicionado.

## 4. IGDB (opcional, para capa/gênero/ano)

1. Entre em <https://dev.twitch.tv/console/apps> (precisa de conta Twitch com 2FA ativado).
2. **Register Your Application**: qualquer nome, OAuth Redirect URL `http://localhost`,
   categoria *Application Integration*, tipo de cliente **Confidential**.
3. Copie o **Client ID** para `IGDB_CLIENT_ID` e gere um **Client Secret** para
   `IGDB_CLIENT_SECRET`.

Teste:

```powershell
python igdb_client.py "Hollow Knight"
```

Sem essas variáveis o bot funciona normalmente, só sem o enriquecimento.

## 5. Supabase (opcional, para ter os dados na nuvem)

O SQLite local continua sendo a fonte da verdade: `/stats` e `/nowplaying` leem dele,
e o bot funciona normalmente sem internet ou sem Supabase. Um worker em background
envia ao Supabase as sessões novas/alteradas (logo após início/fim de sessão e a cada
30s). Se o envio falhar, as linhas ficam pendentes e vão na próxima tentativa
(com espera crescente até 5 min).

1. Crie um projeto em <https://supabase.com/dashboard>.
2. Abra **SQL Editor > New query**, cole o conteúdo de [`supabase/schema.sql`](supabase/schema.sql)
   e clique em **Run**. Isso cria (ou atualiza) a tabela `game_sessions` com RLS ligado
   e sem policies (só a chave secreta acessa). Pode rodar de novo com segurança.
3. Em **Project Settings > Data API**, copie a **Project URL** → `SUPABASE_URL`.
4. Em **Project Settings > API Keys**, copie a chave **secret** (`sb_secret_...`) ou a
   legada **service_role** → `SUPABASE_KEY`.

   Não use a chave publishable/anon: com RLS ligado ela não consegue gravar.
   A chave secreta ignora RLS, então mantenha-a só no `.env`.

Cada linha tem `discord_user_id` (dono da sessão). Enquanto alguém joga, a linha da
sessão aberta tem `ended_at = null` e `last_seen_at` atualizado a cada ~1 min.

## 6. Rodar

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

`Ctrl+C` encerra. Para deixar rodando sem o venv ativado:

```powershell
& "C:\caminho\para\botjogos\.venv\Scripts\python.exe" "C:\caminho\para\botjogos\main.py"
```

## 7. Hospedar no Dokploy

O projeto tem um `Dockerfile`. O bot é um *worker*: não abre porta e não precisa de domínio.

1. No Dokploy: **Create Service > Application**.
2. **Provider:** GitHub, repositório `ludotrack`, branch `main`. **Build Type:** `Dockerfile`.
3. **Environment:** cole as mesmas variáveis do seu `.env` (não precisa de `DATABASE_PATH`,
   o container já usa `/data/games.db`).
4. **Advanced > Volumes/Mounts:** crie um *Volume Mount* com **Mount Path** `/data`.
   Sem isso, o banco SQLite (cadastros e histórico) é apagado a cada deploy.
5. **Deploy.** Nos logs deve aparecer `Bot do Telegram iniciado` e `Conectado ao Discord`.

> ⚠️ Rode **uma instância só**. Se o bot estiver rodando também no seu PC, o Telegram
> recusa uma das duas (`Conflict: terminated by other getUpdates request`) e os
> dois Discords gravariam sessões em dobro. Pare o local antes de subir no Dokploy.

Para levar o histórico que já existe no PC, copie o `games.db` para o volume `/data`
(com o bot parado). Os horários usam `TZ=America/Sao_Paulo`; mude em **Environment** se quiser.

## Como os usuários se cadastram

1. Mandam `/register` no privado do bot do Telegram e recebem um código (vale 10 min)
   e o botão **Adicionar ao Discord**. Se mandarem `/register` num grupo, o bot responde
   com um botão que abre o privado.
2. Pelo botão, adicionam o bot do Discord a um servidor seu (ou entram no servidor oficial,
   se `DISCORD_INVITE_URL` estiver configurado).
3. Nesse servidor, digitam `/vincular CÓDIGO`.
4. Pronto: `/nowplaying` e `/stats` passam a funcionar no privado e nos grupos.

Usar `/register` de novo e vincular outra conta do Discord troca o vínculo.
`/unregister` desvincula; o histórico de sessões fica guardado e volta se a mesma
conta do Discord for vinculada de novo.

## Comandos do Telegram

| Comando               | Onde           | O que faz |
|-----------------------|----------------|-----------|
| `/register`           | privado        | Gera o código para vincular o Discord |
| `/nowplaying` ou `/np`| privado/grupos | Seu jogo atual (ao vivo), com capa, detalhes do Rich Presence e há quanto tempo; ou "não está jogando" + último jogo |
| `/stats`              | privado/grupos | Seus top jogos dos últimos 7 e 30 dias, tempo total, última sessão |
| `/unregister`         | privado/grupos | Desvincula sua conta |
| `/help`               | privado/grupos | Ajuda |

Em grupos com mais de um bot, use `/nowplaying@nome_do_bot`.

## Variáveis opcionais (`.env`)

| Variável             | Padrão     | Descrição |
|----------------------|------------|-----------|
| `DISCORD_INVITE_URL` | (vazio)    | Convite de um servidor "oficial", vira o botão **Entrar no servidor** do `/register` |
| `DISCORD_GUILD_ID`   | (vazio)    | Só para quem vem da versão de um servidor só: remove o `/vincular` antigo duplicado nesse servidor |
| `DATABASE_PATH`      | `games.db` | Caminho do SQLite (relativo à pasta do projeto) |
| `DEBOUNCE_SECONDS`   | `15`       | Tolerância ao fechar um jogo (abrir/trocar vale na hora) |
| `IGNORED_ACTIVITIES` | (vazio)    | Nomes de activities `playing` a ignorar, separados por vírgula. Útil para apps com Rich Presence que não são jogos, ex.: `Visual Studio Code` |
| `LOG_LEVEL`          | `INFO`     | `DEBUG` mostra cada presence update recebido |

## Estrutura

| Arquivo               | Responsabilidade |
|-----------------------|------------------|
| `main.py`             | Entrypoint; liga Discord + Telegram no mesmo loop, grava sessões, heartbeat |
| `discord_presence.py` | Client do Discord (multi-servidor), `/vincular`, filtro por `ActivityType.playing`, tolerância ao fechar, Rich Presence ao vivo |
| `accounts.py`         | Códigos de vinculação e cadastro/remoção de usuários |
| `telegram_bot.py`     | Comandos do Telegram e formatação das respostas |
| `igdb_client.py`      | Busca na IGDB com token Twitch em cache (renova sozinho) e cache de resultados |
| `db.py`               | SQLite: tabelas `users` e `sessions`, estatísticas, controle do que falta sincronizar |
| `supabase_sync.py`    | Envia as sessões pendentes ao Supabase (upsert via REST), com novas tentativas |
| `supabase/schema.sql` | Tabela `game_sessions` para rodar no SQL Editor do Supabase |
| `config.py`           | Leitura do `.env` e configuração de logging |

## Problemas comuns

- **`PrivilegedIntentsRequired`**: ligue PRESENCE INTENT e SERVER MEMBERS INTENT no Developer Portal (passo 2).
- **`/vincular` não aparece no Discord**: adicione o bot de novo pelo link do `/register`
  (ele inclui `applications.commands`) e reinicie o app do Discord (Ctrl+R).
- **"user=... não está em nenhum servidor do bot"**: a pessoa vinculada saiu do servidor
  (ou removeu o bot de lá).
- **O botão "Adicionar ao Discord" não aparece**: o bot do Discord ainda não conectou; tente de novo em instantes.
- **`/nowplaying` diz que não estou jogando**: verifique a privacidade de atividade e se você não
  está invisível.
  Rode com `LOG_LEVEL=DEBUG` para ver se os presence updates chegam.
- **`Supabase recusou a sincronização (HTTP 404)` / `PGRST205`**: a tabela não existe; rode `supabase/schema.sql`.
- **`Supabase recusou ... PGRST204` (coluna não encontrada)**: a tabela é de uma versão antiga; rode `supabase/schema.sql` de novo.
- **`Supabase recusou a sincronização (HTTP 401/403)`**: `SUPABASE_KEY` errada ou é a chave publishable/anon.
