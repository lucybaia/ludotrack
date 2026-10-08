# botjogos — um "Last.fm para jogos"

Bot que acompanha o que você está jogando pela **presence do Discord** e posta no
**Telegram**: avisa quando você começa um jogo (com capa, gênero e ano via IGDB),
avisa quando para (com a duração da sessão) e guarda tudo num SQLite local para
os comandos `/stats` e `/nowplaying`.

Funciona com qualquer jogo que o Discord detecte sozinho (League of Legends,
Valorant, TFT, jogos da Steam/Epic etc.), sem API específica por jogo.

## Como funciona

```
Discord (presence) ──► discord_presence.py ──► main.py (GameTracker) ──► db.py (SQLite)
                         debounce de 15s              │
                                                      └─► fila ──► igdb_client.py ──► telegram_bot.py
```

- Um bot do Discord fica num servidor onde **você também está** e escuta
  `on_presence_update` só do seu user ID.
- Só activities do tipo `playing` contam (Spotify, status personalizado etc. são ignorados).
- **Debounce:** uma mudança (abrir, trocar ou fechar jogo) só vale se durar ≥ 15s.
  Oscilações no início do jogo não viram sessões falsas, e o horário registrado é o
  do início real da mudança.
- Discord e Telegram rodam no **mesmo event loop asyncio** (`asyncio.TaskGroup`).
- Falhas na IGDB só geram log; a mensagem sai sem capa.
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
     - ✅ **PRESENCE INTENT** (obrigatório: é o que permite ver o que você está jogando)
     - ✅ **SERVER MEMBERS INTENT** (para o bot encontrar você na lista de membros)
   - Clique em **Save Changes**.

   Sem esses intents o bot para com o erro `PrivilegedIntentsRequired`.
3. Em **OAuth2 > URL Generator**, marque o scope **bot** (nenhuma permissão é
   necessária), copie a URL gerada, abra no navegador e adicione o bot ao seu servidor.

### O bot precisa estar num servidor em comum com você

O Discord só envia a presence de quem compartilha um servidor com o bot. O mais simples:

1. No Discord, clique em **+** (Adicionar servidor) > **Criar o meu** e crie um servidor
   privado só para isso.
2. Adicione o bot a ele pela URL do passo 3 acima.

### Pegar os IDs

1. No Discord: **Configurações > Avançado > Modo desenvolvedor** (ligar).
2. Botão direito no servidor > **Copiar ID do servidor** → `DISCORD_GUILD_ID`.
3. Botão direito no seu nome > **Copiar ID do usuário** → `DISCORD_USER_ID`.

### Sua atividade precisa estar visível

Em **Configurações > Privacidade de atividade**, deixe ligado
**"Compartilhar sua atividade detectada com outras pessoas"**. Se você estiver
*invisível*, o Discord não envia sua atividade para ninguém, nem para o bot.

### Testar só o Discord

Antes de configurar o resto, dá para rodar só a detecção (só precisa das 3 variáveis do Discord):

```powershell
python discord_presence.py
```

Abra um jogo e veja os logs `Possível mudança ...` e, após 15s, `SESSÃO INICIADA`.
Para ver cada presence update cru, use `$env:LOG_LEVEL = "DEBUG"` antes de rodar.

## 3. Criar o bot no Telegram

1. Fale com o [@BotFather](https://t.me/BotFather), envie `/newbot` e copie o token
   para `TELEGRAM_BOT_TOKEN`.
2. Descubra seu chat ID: mande qualquer mensagem para o seu bot e abra no navegador
   `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`; o número em
   `"chat":{"id": ...}` vai em `TELEGRAM_CHAT_ID`.
   - Para postar num **grupo**, adicione o bot ao grupo e use o ID do grupo (negativo).
   - Para um **canal**, adicione o bot como admin e use `@nome_do_canal`.

Os comandos `/stats` e `/nowplaying` só respondem no chat de `TELEGRAM_CHAT_ID`
(as estatísticas são pessoais). Canais não recebem comandos, então se você postar
num canal os comandos não ficam disponíveis.

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

## 5. Rodar

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

`Ctrl+C` encerra. Para deixar rodando sem o venv ativado:

```powershell
& "C:\caminho\para\botjogos\.venv\Scripts\python.exe" "C:\caminho\para\botjogos\main.py"
```

## Comandos do Telegram

| Comando       | O que faz |
|---------------|-----------|
| `/nowplaying` | Jogo atual e há quanto tempo, ou "offline/sem jogo" |
| `/stats`      | Top jogos dos últimos 7 e 30 dias, tempo total, última sessão |
| `/help`       | Ajuda |

## Variáveis opcionais (`.env`)

| Variável             | Padrão     | Descrição |
|----------------------|------------|-----------|
| `DATABASE_PATH`      | `games.db` | Caminho do SQLite (relativo à pasta do projeto) |
| `DEBOUNCE_SECONDS`   | `15`       | Quanto tempo uma mudança precisa durar para valer |
| `IGNORED_ACTIVITIES` | (vazio)    | Nomes de activities `playing` a ignorar, separados por vírgula. Útil para apps com Rich Presence que não são jogos, ex.: `Visual Studio Code` |
| `LOG_LEVEL`          | `INFO`     | `DEBUG` mostra cada presence update recebido |

## Estrutura

| Arquivo               | Responsabilidade |
|-----------------------|------------------|
| `main.py`             | Entrypoint; liga Discord + Telegram no mesmo loop, grava sessões, fila de notificações, heartbeat |
| `discord_presence.py` | Client do Discord, filtro por `ActivityType.playing`, debounce, detecção de início/fim |
| `igdb_client.py`      | Busca na IGDB com token Twitch em cache (renova sozinho) e cache de resultados |
| `telegram_bot.py`     | Comandos e formatação/envio das mensagens |
| `db.py`               | SQLite: tabela `sessions` e queries de estatística |
| `config.py`           | Leitura do `.env` e configuração de logging |

## Problemas comuns

- **`PrivilegedIntentsRequired`**: ligue PRESENCE INTENT e SERVER MEMBERS INTENT no Developer Portal (passo 2).
- **"O bot não está no servidor"**: confira `DISCORD_GUILD_ID` e se o bot foi convidado.
- **"Usuário ... não encontrado no servidor"**: confira `DISCORD_USER_ID` e se você está no servidor.
- **Nada acontece ao abrir um jogo**: verifique a privacidade de atividade e se você não está invisível.
  Rode com `LOG_LEVEL=DEBUG` para ver se os presence updates chegam.
- **Comandos não respondem no Telegram**: o chat precisa ser o de `TELEGRAM_CHAT_ID`.
