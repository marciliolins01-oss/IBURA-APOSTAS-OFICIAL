import os
import json
import sqlite3
import io
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_PATH = Path(os.getenv('DB_PATH', 'colombia_bot.sqlite3'))
CONFIG_PATH = Path(os.getenv('CONFIG_PATH', 'config.json'))

DEFAULT_CONFIG = {
    "brand_name": "Colombia",
    "accent_color": 0xED1B36,
    "rules_title": "📢 REGRAS PADRÃO - LEIA ANTES DE JOGAR",
    "rules": [
        "Armas: apenas FULL UMP e XM8",
        "1º round: apenas Desert",
        "Proibido mobilador nas filas mobile",
        "Proibido tela parada",
        "Proibido bug de kit"
    ],
    "rules_footer": "Mais agilidade e menos discussões antes das filas.",
    "payment_notice": "UTILIZE PAGO NOME SOBRENOME PRA VALIDAR SEU PAGAMENTO AUTOMATICAMENTE",
    "pix_key": "",
    "pix_type": "E-mail",
    "pix_qr_image_url": "",
    "queue_embed_image_url": "",
    "registration_fee": "R$ 1,00",
    "owner_role_id": 0,
    "mediator_role_id": 0,
    "guild_mediator_role_ids": {},
    "support_role_id": 0,
    "log_channel_id": 0,
    "log_queue_channel_id": 0,
    "log_bets_channel_id": 0,
    "log_bets_open_channel_id": 0,
    "log_bets_closed_channel_id": 0,
    "log_bets_confirmed_channel_id": 0,
    "log_bets_finalized_channel_id": 0,
    "log_match_channel_id": 0,
    "log_mediator_channel_id": 0,
    "log_blacklist_channel_id": 0,
    "log_admin_channel_id": 0,
    "bets_enabled": True,
    "blacklist_channel_id": 0,
    "bets_category_id": 0,
    "mediator_category_id": 0
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding='utf-8')
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        merged = DEFAULT_CONFIG.copy()
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return DEFAULT_CONFIG.copy()

config = load_config()


def save_config():
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')


def mediator_role_id_for(guild_id: int) -> int:
    """Cargo de mediador configurado especificamente para cada servidor."""
    role_map = config.get('guild_mediator_role_ids') or {}
    try:
        return int(role_map.get(str(guild_id), 0) or 0)
    except (TypeError, ValueError):
        return 0


async def resolve_guild_role(guild: discord.Guild, role_id: int):
    """Resolve a role from cache, falling back to Discord's API if cache is stale."""
    if not role_id:
        return None
    role = guild.get_role(int(role_id))
    if role is not None:
        return role
    try:
        roles = await guild.fetch_roles()
        return next((item for item in roles if item.id == int(role_id)), None)
    except (discord.Forbidden, discord.HTTPException):
        return None


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS blacklist (
            player_id TEXT PRIMARY KEY, reason TEXT NOT NULL, added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, actor_id INTEGER,
            action TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            message_id INTEGER, host_id INTEGER NOT NULL, opponent_id INTEGER,
            format TEXT NOT NULL, platform TEXT NOT NULL, mode TEXT NOT NULL, stake TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting', confirmed TEXT NOT NULL DEFAULT '[]',
            mediator_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(matches)').fetchall()}
        if 'match_channel_id' not in columns:
            conn.execute('ALTER TABLE matches ADD COLUMN match_channel_id INTEGER')
        conn.execute('''CREATE TABLE IF NOT EXISTS mediator_service (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS mediator_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            mediator_id INTEGER NOT NULL, amount REAL NOT NULL, description TEXT NOT NULL DEFAULT '',
            registered_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS mediator_pix (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, pix_key TEXT NOT NULL,
            pix_type TEXT NOT NULL DEFAULT 'Aleatória', qr_image_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(guild_id, user_id))''')


def audit(guild_id, actor_id, action, details=''):
    """Salva o evento no SQLite e tenta enviá-lo ao canal de logs apropriado."""
    with db() as conn:
        conn.execute('INSERT INTO audit_log(guild_id, actor_id, action, details) VALUES (?, ?, ?, ?)',
                     (guild_id, actor_id, action, details))
    try:
        if bot.is_ready():
            bot.loop.create_task(send_audit_log(guild_id, actor_id, action, details))
    except (RuntimeError, AttributeError):
        # O registro no banco já foi salvo; falha no envio não deve quebrar a ação principal.
        pass


async def send_audit_log(guild_id, actor_id, action, details=''):
    if not guild_id:
        return
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return
    # Eventos específicos têm canais próprios; se não configurados, usam os logs gerais de apostas.
    if action.startswith(('bet_finalized', 'match_finalized')):
        key, category = 'log_bets_finalized_channel_id', 'APOSTAS FINALIZADAS'
    elif action.startswith(('bet_confirmed', 'queue_confirm')) and ('both=True' in details or action.startswith('bet_confirmed')):
        key, category = 'log_bets_confirmed_channel_id', 'APOSTAS CONFIRMADAS'
    elif action.startswith(('bet_closed', 'queue_close', 'match_cancel')):
        key, category = 'log_bets_closed_channel_id', 'APOSTAS FECHADAS'
    elif action.startswith(('bet_opened', 'queue_create', 'queue_auto_reopen')):
        key, category = 'log_bets_open_channel_id', 'APOSTAS ABERTAS'
    elif action.startswith(('match_',)):
        key, category = 'log_match_channel_id', 'PARTIDAS'
    elif action.startswith(('queue_',)):
        key, category = 'log_queue_channel_id', 'FILAS'
    elif action.startswith(('mediator_',)):
        key, category = 'log_mediator_channel_id', 'MEDIADORES'
    elif action.startswith(('blacklist_',)):
        key, category = 'log_blacklist_channel_id', 'BLACKLIST / PUNIÇÕES'
    elif action.startswith(('payment', 'bet_')):
        key, category = 'log_bets_channel_id', 'APOSTAS'
    else:
        key, category = 'log_admin_channel_id', 'ADMINISTRAÇÃO'
    channel_id = int(config.get(key) or config.get('log_bets_channel_id') or config.get('log_channel_id') or 0)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    # Visual uniforme inspirado no cartão de referência: título forte e detalhes em lista.
    if action.startswith(('bet_opened', 'queue_create')):
        title = 'APOSTA INICIADA'
    elif action.startswith(('bet_confirmed',)) or (action.startswith('queue_confirm') and 'both=True' in details):
        title = 'APOSTA CONFIRMADA'
    elif action.startswith(('bet_closed', 'queue_close', 'match_cancel')):
        title = 'APOSTA FECHADA'
    elif action.startswith(('bet_finalized', 'match_finalized')):
        title = 'APOSTA FINALIZADA'
    elif action.startswith('mediator_'):
        title = 'LOG DE MEDIADORES'
    elif action.startswith('match_'):
        title = 'LOG DA PARTIDA'
    else:
        title = f'LOG • {category}'
    embed = discord.Embed(title=title, color=accent(), timestamp=datetime.now(timezone.utc))
    detail_text = str(details or 'Sem detalhes').replace('; ', '\n• ')
    actor_text = f'<@{actor_id}> (`{actor_id}`)' if actor_id else 'Sistema'
    embed.description = f'• **Ação:** {action}\n• **Responsável:** {actor_text}\n• **Detalhes:** {detail_text}'[:4000]
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


class TranscriptLinkView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='Visualizar Transcript', style=discord.ButtonStyle.link, url=url, emoji='📄'))


async def close_bet_with_transcript(interaction: discord.Interaction, match_id: int, *, finalized=False):
    """Fecha/finaliza uma aposta, publica o transcript apenas no canal de apostas finalizadas e apaga o canal privado."""
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message('Este botão precisa ser usado no canal da partida.', ephemeral=True)
        return
    with db() as conn:
        row = conn.execute('SELECT * FROM matches WHERE id=? AND guild_id=?', (match_id, guild.id)).fetchone()
        if not row:
            await interaction.response.send_message('Aposta não encontrada no banco de dados.', ephemeral=True)
            return
        allowed_ids = {int(row['host_id'] or 0), int(row['opponent_id'] or 0), int(row['mediator_id'] or 0)}
        if interaction.user.id not in allowed_ids and not is_admin(interaction):
            await interaction.response.send_message('Somente os jogadores desta aposta, o mediador responsável ou um administrador podem fechá-la.', ephemeral=True)
            return
        if row['status'] in ('closed', 'finalized', 'cancelled'):
            await interaction.response.send_message('Esta aposta já foi fechada/finalizada.', ephemeral=True)
            return
        new_status = 'finalized' if finalized else 'closed'
        conn.execute('UPDATE matches SET status=? WHERE id=?', (new_status, match_id))
    await interaction.response.defer(ephemeral=True)
    transcript_code = f'COL-{match_id:05d}-{secrets.token_hex(3).upper()}'
    lines = [
        f'TRANSCRIPT COLOMBIA • {transcript_code}', f'Servidor: {guild.name} ({guild.id})',
        f'Canal: #{channel.name} ({channel.id})', f'Aposta: #{match_id}', f'Status final: {new_status}',
        f'Formato: {row["format"]} {row["platform"]}', f'Modo: {row["mode"]}', f'Valor: {row["stake"]}',
        f'Jogadores: {row["host_id"]} e {row["opponent_id"]}', f'Mediador: {row["mediator_id"] or "Não atribuído"}',
        f'Fechada por: {interaction.user} ({interaction.user.id})',
        f'Gerado em UTC: {datetime.now(timezone.utc).isoformat()}', '-' * 72, 'HISTÓRICO DE MENSAGENS', '-' * 72,
    ]
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            stamp = msg.created_at.astimezone(timezone.utc).isoformat()
            content = msg.clean_content or ''
            if msg.embeds:
                for emb in msg.embeds:
                    if emb.title: content += f' [EMBED: {emb.title}]'
                    if emb.description: content += f' {emb.description}'
                    for field in emb.fields: content += f' | {field.name}: {field.value}'
            if msg.attachments: content += ' [ANEXOS: ' + ', '.join(a.url for a in msg.attachments) + ']'
            lines.append(f'[{stamp}] {msg.author} ({msg.author.id}): {content or "[sem texto]"}')
    except (discord.Forbidden, discord.HTTPException) as exc:
        lines.append(f'AVISO: não foi possível ler todo o histórico: {exc}')
    data = ('\n'.join(lines)).encode('utf-8')

    # Transcript é publicado no canal de logs de apostas finalizadas, nunca por mensagem privada.
    log_channel_id = int(config.get('log_bets_finalized_channel_id') or 0)
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    log_message_ok = False
    if isinstance(log_channel, discord.TextChannel):
        status_label = 'APOSTA FINALIZADA' if finalized else 'APOSTA FECHADA'
        embed = discord.Embed(title=status_label, color=accent(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name='AVISO', value='Não definiu o vencedor' if finalized else 'Aposta fechada', inline=False)
        embed.add_field(name='Encerrado por', value=f'<@{interaction.user.id}>', inline=False)
        embed.add_field(name='Mediador', value=f'<@{int(row["mediator_id"])}>' if row['mediator_id'] else 'Não atribuído', inline=False)
        embed.add_field(name='Modo', value=f'{row["mode"]} ({row["format"]} {row["platform"]})', inline=False)
        embed.add_field(name='Valor', value=str(row['stake']), inline=False)
        embed.add_field(name='Código do transcript', value=f'`{transcript_code}`', inline=False)
        try:
            sent = await log_channel.send(embed=embed, file=discord.File(io.BytesIO(data), filename=f'transcript_{transcript_code}.txt'))
            if sent.attachments:
                await sent.edit(view=TranscriptLinkView(sent.attachments[0].url))
            log_message_ok = True
        except (discord.Forbidden, discord.HTTPException) as exc:
            audit(guild.id, interaction.user.id, 'transcript_log_failed', f'match_id={match_id}; error={exc}')
    else:
        audit(guild.id, interaction.user.id, 'transcript_log_channel_missing', f'match_id={match_id}; configured_id={log_channel_id}')

    action = 'bet_finalized' if finalized else 'bet_closed'
    audit(guild.id, interaction.user.id, action, f'match_id={match_id}; code={transcript_code}; transcript_channel={log_channel_id}; transcript_posted={log_message_ok}; channel_deleted=pending')
    await interaction.followup.send(
        f'✅ Aposta {"finalizada" if finalized else "fechada"}. O canal privado será apagado. Transcript publicado no canal de apostas finalizadas: {"sim" if log_message_ok else "não — confira a configuração/permissões do canal de logs"}.',
        ephemeral=True
    )
    try:
        await channel.delete(reason=f'Aposta #{match_id} fechada; transcript {transcript_code} arquivado nos logs')
        audit(guild.id, interaction.user.id, action, f'match_id={match_id}; code={transcript_code}; channel_deleted=true')
    except (discord.Forbidden, discord.HTTPException) as exc:
        audit(guild.id, interaction.user.id, 'channel_delete_failed', f'match_id={match_id}; error={exc}')


def is_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id)


def accent():
    return discord.Color(int(config.get('accent_color', 0xED1B36)))


def rules_embed():
    lines = '\n'.join(f'→ {line}' for line in config.get('rules', []))
    embed = discord.Embed(title=config.get('rules_title', '📢 REGRAS PADRÃO - LEIA ANTES DE JOGAR'),
                          description=f"A partir de agora, todas as filas seguirão:\n\n{lines}\n\nNão será mais necessário fazer combinações ou enviar placas para essas regras.\n\n🔴 {config.get('rules_footer', '')}",
                          color=accent())
    return embed


def payment_embed():
    embed = discord.Embed(title='Chave PIX', color=accent())
    key = config.get('pix_key') or 'Chave PIX ainda não configurada pelo administrador.'
    embed.description = f"**Chave:** `{key}`\n**Tipo:** `{config.get('pix_type', 'E-mail')}`"
    qr = config.get('pix_qr_image_url')
    if qr:
        embed.set_image(url=qr)
    return embed


class QueueView(discord.ui.View):
    """Fila pública: qualquer jogador pode entrar; os dois confirmam depois."""
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        join = discord.ui.Button(label='✅ Entrar na Fila', style=discord.ButtonStyle.success, custom_id=f'colombia:q:{match_id}:join')
        leave = discord.ui.Button(label='❌ Sair da Fila', style=discord.ButtonStyle.danger, custom_id=f'colombia:q:{match_id}:leave')
        join.callback = self.join_queue
        leave.callback = self.leave_queue
        self.add_item(join); self.add_item(leave)

    def make_embed(self, row):
        players = ([row['host_id']] if row['host_id'] and row['host_id'] != 0 else []) + ([row['opponent_id']] if row['opponent_id'] else [])
        confirmed = json.loads(row['confirmed'] or '[]')
        embed = discord.Embed(title='APOSTA INICIADA', color=accent())
        player_lines = '\n'.join(f'<@{uid}>' for uid in players) if players else 'Nenhum jogador na fila'
        embed.description = player_lines
        # Campos em linha deixam o embed mais baixo e aproveitam melhor a largura disponível.
        embed.add_field(name='Modo', value=f'{row["format"]} {row["platform"]} • {row["mode"]}', inline=True)
        embed.add_field(name='Valor', value=str(row['stake']), inline=True)
        embed.add_field(name='Jogadores em fila', value=f'{len(players)}/2', inline=True)
        embed.set_footer(text='Aguardando jogador...' if not row['opponent_id'] else 'Dupla formada • negociação no canal privado')
        queue_image = str(config.get('queue_embed_image_url', '') or '').strip()
        if queue_image.startswith(('https://', 'http://')):
            embed.set_thumbnail(url=queue_image)
        return embed

    async def refresh(self, interaction, row):
        await interaction.response.edit_message(embed=self.make_embed(row), view=self if row['status'] == 'waiting' else None)

    async def join_queue(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.user.bot:
            await interaction.response.send_message('Somente jogadores podem entrar na fila.', ephemeral=True); return
        with db() as conn:
            row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            if not row or row['status'] != 'waiting':
                await interaction.response.send_message('Esta fila não está aberta.', ephemeral=True); return
            if interaction.user.id in (row['host_id'], row['opponent_id']) and interaction.user.id != 0:
                await interaction.response.send_message('Você já está nesta fila.', ephemeral=True); return
            if row['host_id'] and row['opponent_id']:
                await interaction.response.send_message('Esta fila já tem dois jogadores.', ephemeral=True); return
            if not row['host_id']:
                conn.execute('UPDATE matches SET host_id=? WHERE id=?', (interaction.user.id, self.match_id))
            else:
                conn.execute('UPDATE matches SET opponent_id=? WHERE id=?', (interaction.user.id, self.match_id))
            row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            paired = bool(row['host_id'] and row['opponent_id'])
            if paired:
                # Fecha esta vaga imediatamente; a próxima dupla usa outra fila publicada.
                conn.execute("UPDATE matches SET status='negotiating' WHERE id=? AND status='waiting'", (self.match_id,))
                row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
        audit(interaction.guild_id, interaction.user.id, 'queue_join', f'match_id={self.match_id}; paired={paired}')
        await self.refresh(interaction, row)
        if paired:
            try:
                channel = await create_mediator_channel(interaction.guild, self.match_id)
                if channel:
                    # Reabre automaticamente uma nova fila com as mesmas regras para a próxima dupla.
                    with db() as conn:
                        cur = conn.execute(
                            'INSERT INTO matches(guild_id, channel_id, host_id, opponent_id, format, platform, mode, stake, status, confirmed) VALUES (?, ?, 0, NULL, ?, ?, ?, ?, ?, ?)',
                            (interaction.guild_id, interaction.channel_id, row['format'], row['platform'], row['mode'], row['stake'], 'waiting', '[]')
                        )
                        next_match_id = cur.lastrowid
                    next_embed = discord.Embed(
                        title=f'{row["format"]} {row["platform"]}',
                        color=accent()
                    )
                    # Campos em linha deixam a fila automática mais baixa e compacta.
                    next_embed.add_field(name='Modo', value=str(row['mode']), inline=True)
                    next_embed.add_field(name='Valor', value=str(row['stake']), inline=True)
                    next_embed.add_field(name='Jogadores em fila', value='0/2', inline=True)
                    next_embed.set_footer(text='Aguardando jogador...')
                    queue_image = str(config.get('queue_embed_image_url', '') or '').strip()
                    if queue_image.startswith(('https://', 'http://')):
                        next_embed.set_thumbnail(url=queue_image)
                    next_msg = await interaction.channel.send(embed=next_embed, view=QueueView(next_match_id))
                    with db() as conn:
                        conn.execute('UPDATE matches SET message_id=? WHERE id=?', (next_msg.id, next_match_id))
                    audit(interaction.guild_id, interaction.user.id, 'bet_opened', f'fila automática; previous_match={self.match_id}; next_match={next_match_id}')
                    await interaction.followup.send(f'🤝 A dupla foi formada! Negociem no canal privado {channel.mention}. Já publiquei uma nova fila para os próximos jogadores.', ephemeral=True)
                else:
                    await interaction.followup.send('A dupla foi formada, mas o canal privado já existe ou não foi criado. Avise a equipe.', ephemeral=True)
            except (discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
                audit(interaction.guild_id, interaction.user.id, 'mediator_channel_create_error', f'match_id={self.match_id}; erro={exc}')
                await interaction.followup.send(f'⚠️ A dupla foi formada, mas não consegui criar o canal privado. Confira as permissões do bot. Detalhe: {str(exc)[:500]}', ephemeral=True)

    async def confirm_bet(self, interaction: discord.Interaction):
        await interaction.response.send_message('A confirmação agora é feita no canal privado da partida, depois que os dois jogadores forem pareados.', ephemeral=True)

    async def leave_queue(self, interaction: discord.Interaction):
        with db() as conn:
            row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            if not row or row['status'] != 'waiting':
                await interaction.response.send_message('Esta fila não pode mais ser alterada.', ephemeral=True); return
            if interaction.user.id == row['host_id']:
                if row['opponent_id']:
                    conn.execute('UPDATE matches SET host_id=opponent_id, opponent_id=NULL, confirmed=? WHERE id=?', (json.dumps([]), self.match_id))
                else:
                    conn.execute('UPDATE matches SET host_id=0, confirmed=? WHERE id=?', (json.dumps([]), self.match_id))
                row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            elif interaction.user.id == row['opponent_id']:
                conn.execute('UPDATE matches SET opponent_id=NULL, confirmed=? WHERE id=?', (json.dumps([u for u in json.loads(row['confirmed'] or '[]') if u != interaction.user.id]), self.match_id))
                row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            else:
                await interaction.response.send_message('Você não está nesta fila.', ephemeral=True); return
        await interaction.response.edit_message(content='❌ Fila cancelada pelo criador.' if row['status']=='cancelled' else None, embed=self.make_embed(row), view=None if row['status']=='cancelled' else self)


def mediator_is_on_duty(guild_id: int, user_id: int) -> bool:
    with db() as conn:
        row = conn.execute('SELECT active FROM mediator_service WHERE guild_id=? AND user_id=?', (guild_id, user_id)).fetchone()
    return bool(row and row['active'])


async def refresh_mediator_channel_access(guild: discord.Guild, channel: discord.TextChannel, match_id: int):
    """Only on-duty mediators can see unclaimed bets; after claim only the owner can see it."""
    with db() as conn:
        match = conn.execute('SELECT mediator_id, status FROM matches WHERE id=?', (match_id,)).fetchone()
        services = conn.execute('SELECT user_id FROM mediator_service WHERE guild_id=? AND active=1', (guild.id,)).fetchall()
    if not match:
        return
    role_id = mediator_role_id_for(guild.id)
    role = await resolve_guild_role(guild, role_id)
    if role:
        await channel.set_permissions(role, view_channel=False, send_messages=False, read_message_history=False)
    # Clear permissions for mediators who are no longer on duty / did not claim.
    mediator_ids = {m.id for m in role.members} if role else set()
    mediator_ids.update(int(r['user_id']) for r in services)
    for user_id in mediator_ids:
        member = guild.get_member(user_id)
        if not member:
            continue
        if match['mediator_id']:
            allowed = user_id == int(match['mediator_id'])
        else:
            allowed = mediator_is_on_duty(guild.id, user_id) and bool(role and any(r.id == role.id for r in member.roles))
        await channel.set_permissions(member, view_channel=allowed, send_messages=allowed, read_message_history=allowed)


class MediatorClaimView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        button = discord.ui.Button(label='🎭 Assumir aposta', style=discord.ButtonStyle.success, custom_id=f'colombia:med:{match_id}:claim')
        button.callback = self.claim
        self.add_item(button)
        finish_button = discord.ui.Button(label='🏁 Finalizar aposta', style=discord.ButtonStyle.primary,
                                          custom_id=f'colombia:bet:{match_id}:finalize', row=1)
        finish_button.callback = self.finalize_click
        self.add_item(finish_button)

    async def finalize_click(self, interaction: discord.Interaction):
        with db() as conn:
            row = conn.execute('SELECT mediator_id FROM matches WHERE id=? AND guild_id=?', (self.match_id, interaction.guild_id)).fetchone()
        if not row or (interaction.user.id != row['mediator_id'] and not is_admin(interaction)):
            await interaction.response.send_message('Somente o mediador responsável ou um administrador pode finalizar a aposta.', ephemeral=True)
            return
        await close_bet_with_transcript(interaction, self.match_id, finalized=True)

    async def claim(self, interaction: discord.Interaction):
        role_id = mediator_role_id_for(interaction.guild_id or 0)
        if not role_id or not isinstance(interaction.user, discord.Member) or not any(r.id == role_id for r in interaction.user.roles):
            await interaction.response.send_message('Somente quem tem o cargo de mediador configurado pode assumir.', ephemeral=True); return
        if not mediator_is_on_duty(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message('Você precisa entrar em serviço antes de assumir uma aposta. Use /servico_mediador.', ephemeral=True); return
        with db() as conn:
            row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            if not row or row['mediator_id'] or row['status'] not in ('confirmed', 'payment'):
                await interaction.response.send_message('Esta aposta já foi assumida ou não está disponível.', ephemeral=True); return
            # Atomic conditional update: only the first mediator can claim the bet.
            cur = conn.execute("UPDATE matches SET mediator_id=?, status='payment' WHERE id=? AND mediator_id IS NULL AND status IN ('confirmed','payment')", (interaction.user.id, self.match_id))
            if cur.rowcount != 1:
                await interaction.response.send_message('Outro mediador acabou de assumir esta aposta.', ephemeral=True); return
        await interaction.response.defer()
        if isinstance(interaction.channel, discord.TextChannel):
            await refresh_mediator_channel_access(interaction.guild, interaction.channel, self.match_id)
        embed = discord.Embed(title='💳 Pagamento da aposta', description=f"Jogadores: <@{row['host_id']}> e <@{row['opponent_id']}>\nValor: **{row['stake']}**\n\n{config.get('payment_notice','')}\n\nO mediador deve conferir os comprovantes manualmente.", color=accent())
        await interaction.followup.send(content=f"✅ Mediador responsável: {interaction.user.mention}", embed=embed)
        await interaction.followup.send(embed=mediator_payment_embed(interaction.guild.id, interaction.user.id))
        audit(interaction.guild_id, interaction.user.id, 'mediator_claim', f'match_id={self.match_id}')
        audit(interaction.guild_id, interaction.user.id, 'match_mediator_assigned', f'match_id={self.match_id}')


async def _get_member_for_channel(guild: discord.Guild, user_id: int):
    """Resolve um jogador mesmo quando ele não está no cache de membros."""
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(user_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


class PlayerBetConfirmView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        button = discord.ui.Button(label='🤝 CONFIRMAR APOSTA', style=discord.ButtonStyle.success,
                                   custom_id=f'colombia:bet:{match_id}:confirm')
        button.callback = self.confirm_click
        self.add_item(button)

    async def confirm_click(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message('Use este botão dentro do servidor.', ephemeral=True)
            return
        with db() as conn:
            row = conn.execute('SELECT * FROM matches WHERE id=? AND guild_id=?', (self.match_id, interaction.guild.id)).fetchone()
            if not row or row['status'] not in ('negotiating', 'waiting'):
                await interaction.response.send_message('Esta aposta não está aguardando confirmação dos jogadores.', ephemeral=True)
                return
            if interaction.user.id not in (row['host_id'], row['opponent_id']):
                await interaction.response.send_message('Somente os dois jogadores desta partida podem confirmar.', ephemeral=True)
                return
            confirmed = json.loads(row['confirmed'] or '[]')
            new_confirmation = interaction.user.id not in confirmed
            if new_confirmation:
                confirmed.append(interaction.user.id)
            both = row['host_id'] in confirmed and row['opponent_id'] in confirmed
            new_status = 'confirmed' if both else 'negotiating'
            conn.execute('UPDATE matches SET confirmed=?, status=? WHERE id=?', (json.dumps(confirmed), new_status, self.match_id))
        audit(interaction.guild.id, interaction.user.id, 'bet_confirmed' if both else 'queue_confirm', f'match_id={self.match_id}; both={both}')
        # Mostra o progresso publicamente no canal da aposta, sem repetir se o jogador clicar novamente.
        if new_confirmation and isinstance(interaction.channel, discord.TextChannel):
            count = sum(1 for user_id in (row['host_id'], row['opponent_id']) if user_id in confirmed)
            verb = 'confirmou' if count == 1 else 'confirmaram'
            try:
                await interaction.channel.send(f'✅ **{count}/2 jogadores {verb} a aposta!**')
            except (discord.Forbidden, discord.HTTPException):
                pass
        if both:
            embed = discord.Embed(
                title=f'✅ Aposta #{self.match_id} confirmada',
                description=(f"Os dois jogadores confirmaram.\n\n**Jogadores:** <@{row['host_id']}> e <@{row['opponent_id']}>"
                             f"\n**Formato:** {row['format']} {row['platform']}\n**Modo:** {row['mode']}\n**Valor:** {row['stake']}"
                             "\n\nAguardando um mediador em serviço assumir a aposta."),
                color=accent()
            )
            await interaction.response.edit_message(embed=embed, view=MediatorClaimView(self.match_id))
            await interaction.followup.send('✅ Os dois jogadores confirmaram a aposta. Um mediador em serviço poderá assumir agora.', ephemeral=True)
        else:
            await interaction.response.send_message(f'✅ Sua confirmação foi registrada. Aguardando o outro jogador. ({len(confirmed)}/2)', ephemeral=True)


async def create_mediator_channel(guild: discord.Guild, match_id: int):
    with db() as conn:
        row = conn.execute('SELECT * FROM matches WHERE id=? AND guild_id=?', (match_id, guild.id)).fetchone()
        active_rows = conn.execute('SELECT user_id FROM mediator_service WHERE guild_id=? AND active=1', (guild.id,)).fetchall()
    if not row or row['match_channel_id']:
        return None

    # Resolve todos os alvos de permissão antes de montar overwrites: discord.py não aceita chave None.
    host = await _get_member_for_channel(guild, row['host_id']) if row['host_id'] else None
    opponent = await _get_member_for_channel(guild, row['opponent_id']) if row['opponent_id'] else None
    if host is None or opponent is None:
        raise RuntimeError('Não consegui localizar os dois jogadores no servidor. Confira se ambos continuam no servidor e tente novamente.')

    bot_member = guild.me
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    if bot_member is None:
        raise RuntimeError('Não consegui localizar o membro do bot neste servidor.')

    role_id = mediator_role_id_for(guild.id)
    mediator_role = await resolve_guild_role(guild, role_id)
    category_id = int(config.get('mediator_category_id') or config.get('bets_category_id') or 0)
    category = guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        host: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        opponent: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    # O cargo inteiro fica sem acesso; apenas mediadores ativos recebem acesso individual.
    if mediator_role is not None:
        overwrites[mediator_role] = discord.PermissionOverwrite(view_channel=False, send_messages=False, read_message_history=False)
        for item in active_rows:
            member = await _get_member_for_channel(guild, int(item['user_id']))
            if member is not None and any(role.id == mediator_role.id for role in member.roles):
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    name = f'aposta-{match_id:04d}'
    channel = await guild.create_text_channel(
        name, category=category, overwrites=overwrites,
        reason=f'Aposta #{match_id} confirmada pelos jogadores'
    )
    with db() as conn:
        conn.execute('UPDATE matches SET match_channel_id=? WHERE id=? AND guild_id=?', (channel.id, match_id, guild.id))
    embed = discord.Embed(
        title=f'🎮 Aposta #{match_id} — negociação entre jogadores',
        description=(f"**Jogadores:** <@{row['host_id']}> e <@{row['opponent_id']}>\n"
                     f"**Formato:** {row['format']} {row['platform']}\n**Modo:** {row['mode']}\n**Valor:** {row['stake']}\n\n"
                     'Negociem aqui as regras da partida. Quando ambos estiverem de acordo, cada jogador deve clicar em **CONFIRMAR APOSTA**. '),
        color=accent()
    )
    await channel.send(content=f"<@{row['host_id']}> <@{row['opponent_id']}>", embed=embed, view=PlayerBetConfirmView(match_id))
    audit(guild.id, None, 'mediator_channel_create', f'match_id={match_id}; channel={channel.id}')
    audit(guild.id, None, 'match_created', f'match_id={match_id}; channel={channel.id}; host={row["host_id"]}; opponent={row["opponent_id"]}')
    return channel


class MatchActions(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label='Regras ↗', style=discord.ButtonStyle.secondary, custom_id='colombia:match:rules')
    async def rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=rules_embed(), ephemeral=True)

    @discord.ui.button(label='▦ QR', style=discord.ButtonStyle.secondary, custom_id='colombia:match:payment')
    async def payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=payment_embed(), ephemeral=True)

    @discord.ui.button(label='Cancelar', style=discord.ButtonStyle.danger, custom_id='colombia:match:cancel')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        with db() as conn:
            row = conn.execute('SELECT * FROM matches WHERE id=?', (self.match_id,)).fetchone()
            if not row:
                await interaction.response.send_message('Partida não encontrada.', ephemeral=True)
                return
            if interaction.user.id not in (row['host_id'], row['mediator_id']) and not is_admin(interaction):
                await interaction.response.send_message('Somente o criador, mediador ou administrador pode cancelar.', ephemeral=True)
                return
            conn.execute("UPDATE matches SET status='cancelled' WHERE id=?", (self.match_id,))
        audit(interaction.guild_id, interaction.user.id, 'match_cancel', f'match_id={self.match_id}')
        await interaction.response.edit_message(content='❌ Partida cancelada.', embed=None, view=None)


class ColombiaBot(commands.Bot):
    async def setup_hook(self):
        init_db()
        self.add_view(BlacklistPanelView())
        self.add_view(MediatorAvailabilityPanelView())
        # Sync persistent views for messages that remain active after a restart.
        with db() as conn:
            rows = conn.execute("SELECT id, status FROM matches WHERE status IN ('waiting', 'negotiating', 'confirmed', 'payment')").fetchall()
        for row in rows:
            if row['status'] == 'waiting': self.add_view(QueueView(row['id']))
            if row['status'] == 'negotiating': self.add_view(PlayerBetConfirmView(row['id']))
            if row['status'] in ('confirmed', 'payment'): self.add_view(MediatorClaimView(row['id']))
            self.add_view(MatchActions(row['id']))
        await self.tree.sync()

intents = discord.Intents.default()
bot = ColombiaBot(command_prefix='!', intents=intents)

@bot.tree.command(name='servico_mediador', description='Entra ou sai do serviço de mediador.')
async def servico_mediador(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message('Use este comando dentro do servidor.', ephemeral=True); return
    role_id = mediator_role_id_for(interaction.guild_id or 0)
    if not role_id or not any(r.id == role_id for r in interaction.user.roles):
        await interaction.response.send_message('Este comando é exclusivo para quem tem o cargo de mediador configurado.', ephemeral=True); return
    active = not mediator_is_on_duty(interaction.guild_id, interaction.user.id)
    await update_mediator_service(interaction.guild, interaction.user, active)
    if active:
        await interaction.response.send_message('🟢 Você entrou em serviço. Novas apostas e apostas aguardando mediador ficarão visíveis para você; quem assumir primeiro fica responsável.', ephemeral=True)
    else:
        await interaction.response.send_message('⚫ Você saiu de serviço. Você não verá novas apostas sem mediador. Se já assumiu uma aposta, continuará com acesso a ela.', ephemeral=True)



def brl(value: float) -> str:
    return ('R$ {:,.2f}'.format(float(value))).replace(',', 'X').replace('.', ',').replace('X', '.')


def mediator_role_member(member: discord.Member) -> bool:
    role_id = mediator_role_id_for(member.guild.id)
    return bool(role_id and any(role.id == role_id for role in member.roles))


async def mediator_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title='🎭 Mediadores disponíveis',
        description='Utilize este painel para gerenciar seu status de serviço e visualizar seus rendimentos.',
        color=accent()
    )
    with db() as conn:
        rows = conn.execute(
            'SELECT user_id FROM mediator_service WHERE guild_id=? AND active=1 ORDER BY updated_at ASC',
            (guild.id,)
        ).fetchall()
    online = []
    for row in rows:
        member = guild.get_member(int(row['user_id']))
        if member is None:
            try:
                member = await guild.fetch_member(int(row['user_id']))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        if mediator_role_member(member):
            online.append(member)
    embed.add_field(
        name=f'🟢 Mediadores Online ({len(online)})',
        value='\n'.join(f'• {member.mention}' for member in online) if online else '• Nenhum mediador disponível no momento.',
        inline=False
    )
    embed.set_footer(text=f"{config.get('brand_name', 'Colombia')} • Painel de mediadores")
    return embed


async def update_mediator_service(guild: discord.Guild, member: discord.Member, active: bool):
    with db() as conn:
        conn.execute(
            '''INSERT INTO mediator_service(guild_id, user_id, active, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET active=excluded.active, updated_at=CURRENT_TIMESTAMP''',
            (guild.id, member.id, int(active))
        )
        pending = conn.execute(
            "SELECT id, match_channel_id FROM matches WHERE guild_id=? AND mediator_id IS NULL AND status IN ('confirmed','payment') AND match_channel_id IS NOT NULL",
            (guild.id,)
        ).fetchall()
    for item in pending:
        channel = guild.get_channel(int(item['match_channel_id']))
        if isinstance(channel, discord.TextChannel):
            try:
                await refresh_mediator_channel_access(guild, channel, int(item['id']))
            except (discord.Forbidden, discord.HTTPException):
                pass
    audit(guild.id, member.id, 'mediator_service', 'on' if active else 'off')


class MediatorPixModal(discord.ui.Modal, title='Configuração de Chave PIX'):
    pix_type = discord.ui.TextInput(label='Tipo da chave PIX', placeholder='Ex.: CPF', max_length=30)
    pix_key = discord.ui.TextInput(label='Sua chave PIX', placeholder='Digite sua chave PIX', max_length=200)
    qr_url = discord.ui.TextInput(label='URL da imagem do QR Code (opcional)', placeholder='https://...', required=False, max_length=500)

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message('Use este formulário no servidor onde abriu o painel.', ephemeral=True)
            return
        kind = self.pix_type.value.strip()
        key = self.pix_key.value.strip()
        qr = self.qr_url.value.strip()
        if not key:
            await interaction.response.send_message('A chave PIX não pode ficar vazia.', ephemeral=True)
            return
        if qr and not (qr.startswith('https://') or qr.startswith('http://')):
            await interaction.response.send_message('A URL do QR Code deve começar com http:// ou https://.', ephemeral=True)
            return
        with db() as conn:
            conn.execute('''INSERT INTO mediator_pix(guild_id,user_id,pix_key,pix_type,qr_image_url,updated_at)
                VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(guild_id,user_id) DO UPDATE SET
                pix_key=excluded.pix_key,pix_type=excluded.pix_type,qr_image_url=excluded.qr_image_url,updated_at=CURRENT_TIMESTAMP''',
                (self.guild_id, interaction.user.id, key, kind or 'Aleatória', qr))
        audit(self.guild_id, interaction.user.id, 'mediator_pix_update', f'type={kind}; qr_image={bool(qr)}')
        await interaction.response.send_message('✅ Sua chave PIX foi salva. Ela é individual e só você pode visualizar pelo painel; será usada nas informações de pagamento das apostas que você assumir.', ephemeral=True)


def mediator_pix_embed(guild_id: int, mediator_id: int) -> discord.Embed:
    with db() as conn:
        row = conn.execute('SELECT pix_key,pix_type,qr_image_url,updated_at FROM mediator_pix WHERE guild_id=? AND user_id=?', (guild_id, mediator_id)).fetchone()
    embed = discord.Embed(title='💳 Minha configuração de Chave PIX', color=accent())
    if not row:
        embed.description = 'Você ainda não cadastrou sua chave PIX. Clique em **CONFIGURAR MEU PIX** no painel de mediadores.'
        return embed
    embed.description = f"**Chave:** `{row['pix_key']}`\n**Tipo:** `{row['pix_type']}`\n**Atualizada em:** {row['updated_at']}"
    if row['qr_image_url']:
        embed.set_image(url=row['qr_image_url'])
    return embed


def mediator_payment_embed(guild_id: int, mediator_id: int) -> discord.Embed:
    with db() as conn:
        row = conn.execute('SELECT pix_key,pix_type,qr_image_url FROM mediator_pix WHERE guild_id=? AND user_id=?', (guild_id, mediator_id)).fetchone()
    if not row:
        return payment_embed()
    embed = discord.Embed(title='💳 Chave PIX do mediador responsável', description=f"**Chave:** `{row['pix_key']}`\n**Tipo:** `{row['pix_type']}`\n\n{config.get('payment_notice','')}\n\nO mediador deve conferir os comprovantes manualmente.", color=accent())
    if row['qr_image_url']:
        embed.set_image(url=row['qr_image_url'])
    return embed


class MediatorAvailabilityPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _require_mediator(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Use este painel dentro do servidor.', ephemeral=True)
            return False
        role_id = mediator_role_id_for(interaction.guild.id)
        if not role_id:
            await interaction.response.send_message(
                '❌ O cargo de mediador ainda não foi configurado. Use `/configurar_cargo_mediador` e selecione o cargo neste servidor.',
                ephemeral=True
            )
            return False
        # Prefer the role IDs on the member object, then resolve the role via cache/API.
        has_role = any(role.id == role_id for role in interaction.user.roles)
        configured_role = await resolve_guild_role(interaction.guild, role_id)
        if has_role and configured_role is not None:
            return True
        if configured_role is None:
            detail = (f'Não consegui localizar o cargo ID `{role_id}` neste servidor nem consultando o Discord. '
                      'Confirme que o comando de configuração foi executado no servidor deste painel.')
        else:
            member_role_ids = ', '.join(str(role.id) for role in interaction.user.roles if role.name != '@everyone') or 'nenhum'
            detail = (f'O cargo configurado é {configured_role.mention} (ID `{role_id}`), mas sua conta não possui esse cargo. '
                      f'IDs dos cargos da sua conta: `{member_role_ids}`.')
        await interaction.response.send_message('❌ Não foi possível validar seu cargo de mediador.\n' + detail, ephemeral=True)
        return False
        return True

    async def _refresh_panel(self, interaction: discord.Interaction):
        if interaction.message and interaction.guild:
            try:
                await interaction.message.edit(embed=await mediator_panel_embed(interaction.guild), view=self)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @discord.ui.button(label='ENTRAR EM FILA', emoji='🟢', style=discord.ButtonStyle.success, custom_id='colombia:mediatorpanel:join', row=0)
    async def join_service(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_mediator(interaction):
            return
        await update_mediator_service(interaction.guild, interaction.user, True)
        await self._refresh_panel(interaction)
        await interaction.response.send_message('🟢 Você entrou na fila de mediadores e está disponível para assumir apostas.', ephemeral=True)

    @discord.ui.button(label='SAIR DA FILA', emoji='⚫', style=discord.ButtonStyle.danger, custom_id='colombia:mediatorpanel:leave', row=0)
    async def leave_service(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_mediator(interaction):
            return
        await update_mediator_service(interaction.guild, interaction.user, False)
        await self._refresh_panel(interaction)
        await interaction.response.send_message('⚫ Você saiu da fila de mediadores. Se já assumiu uma aposta, continuará com acesso a ela.', ephemeral=True)

    @discord.ui.button(label='RANKING', emoji='🏆', style=discord.ButtonStyle.primary, custom_id='colombia:mediatorpanel:ranking', row=1)
    async def ranking(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message('Use este botão dentro do servidor.', ephemeral=True)
            return
        with db() as conn:
            rows = conn.execute(
                '''SELECT mediator_id, SUM(amount) AS total, COUNT(*) AS count
                   FROM mediator_earnings WHERE guild_id=? GROUP BY mediator_id
                   ORDER BY total DESC, count DESC LIMIT 10''', (interaction.guild.id,)
            ).fetchall()
        lines = []
        for idx, row in enumerate(rows, 1):
            lines.append(f"**{idx}.** <@{int(row['mediator_id'])}> — **{brl(row['total'] or 0)}** ({row['count']} registros)")
        embed = discord.Embed(title='🏆 Ranking de mediadores', description='\n'.join(lines) if lines else 'Ainda não há receitas registradas.', color=accent())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label='MINHA RECEITA', emoji='💰', style=discord.ButtonStyle.secondary, custom_id='colombia:mediatorpanel:revenue', row=1)
    async def my_revenue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message('Use este botão dentro do servidor.', ephemeral=True)
            return
        with db() as conn:
            row = conn.execute(
                'SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count FROM mediator_earnings WHERE guild_id=? AND mediator_id=?',
                (interaction.guild.id, interaction.user.id)
            ).fetchone()
            recent = conn.execute(
                'SELECT amount, description, created_at FROM mediator_earnings WHERE guild_id=? AND mediator_id=? ORDER BY id DESC LIMIT 5',
                (interaction.guild.id, interaction.user.id)
            ).fetchall()
        details = '\n'.join(f"• {brl(item['amount'])} — {item['description'] or 'Receita registrada'} ({item['created_at']})" for item in recent)
        embed = discord.Embed(title='💰 Minha receita', description=f"**Total registrado:** {brl(row['total'])}\n**Lançamentos:** {row['count']}\n\n**Últimos registros**\n{details or 'Nenhum lançamento registrado ainda.'}", color=accent())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label='CONFIGURAR MEU PIX', emoji='💳', style=discord.ButtonStyle.success, custom_id='colombia:mediatorpanel:pix_setup', row=2)
    async def setup_my_pix(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_mediator(interaction):
            return
        await interaction.response.send_modal(MediatorPixModal(interaction.guild.id))

    @discord.ui.button(label='VER MEU PIX', emoji='🔒', style=discord.ButtonStyle.secondary, custom_id='colombia:mediatorpanel:pix_view', row=2)
    async def view_my_pix(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_mediator(interaction):
            return
        await interaction.response.send_message(embed=mediator_pix_embed(interaction.guild.id, interaction.user.id), ephemeral=True)


@bot.tree.command(name='painel_mediadores', description='Publica o painel de disponibilidade, ranking e receita dos mediadores.')
async def painel_mediadores(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message('Use este comando em um canal de texto do servidor.', ephemeral=True)
        return
    await interaction.response.send_message('✅ Painel de mediadores publicado.', ephemeral=True)
    await interaction.channel.send(embed=await mediator_panel_embed(interaction.guild), view=MediatorAvailabilityPanelView())
    audit(interaction.guild_id, interaction.user.id, 'mediator_panel_publish', f'channel={interaction.channel_id}')


@bot.tree.command(name='registrar_receita', description='Registra um rendimento de mediador para o ranking.')
@app_commands.describe(mediador='Mediador que recebeu o valor', valor='Valor recebido em reais', descricao='Referência ou motivo do recebimento')
async def registrar_receita(interaction: discord.Interaction, mediador: discord.Member, valor: app_commands.Range[float, 0.01, 1000000.0], descricao: str = 'Receita de mediação'):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem registrar receitas.', ephemeral=True)
        return
    if not mediator_role_member(mediador):
        await interaction.response.send_message('O usuário selecionado não possui o cargo de mediador configurado.', ephemeral=True)
        return
    with db() as conn:
        conn.execute(
            'INSERT INTO mediator_earnings(guild_id, mediator_id, amount, description, registered_by) VALUES (?, ?, ?, ?, ?)',
            (interaction.guild_id, mediador.id, float(valor), descricao[:200], interaction.user.id)
        )
    audit(interaction.guild_id, interaction.user.id, 'mediator_earning_register', f'mediator={mediador.id}; amount={float(valor):.2f}; description={descricao[:200]}')
    await interaction.response.send_message(f'✅ Registrado **{brl(float(valor))}** para {mediador.mention}.', ephemeral=True)


@bot.event
async def on_ready():
    print(f'Conectado como {bot.user} ({bot.user.id})')
    # Evita iniciar o mesmo loop novamente quando o Discord reconectar.
    if not queue_refresh_loop.is_running():
        queue_refresh_loop.start()

@bot.tree.command(name='painel', description='Publica o painel administrativo do bot.')
async def painel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem abrir este painel.', ephemeral=True)
        return
    embed = discord.Embed(title='SY NEX BOT • Painel de Configuração', color=accent())
    embed.description = (
        f"**Moedas por vitória**\n1\n\n**Modo da fila de mediadores**\nFila em loop (rodízio)\n\n"
        f"**Aviso de pagamento**\n{config.get('payment_notice')}\n\n**Apostas ativas**\n{'✅ Sim' if config.get('bets_enabled') else '❌ Não'}"
    )
    await interaction.response.send_message(embed=embed, view=AdminPanel(), ephemeral=True)

class QueueImageURLModal(discord.ui.Modal, title='Imagem do embed da fila'):
    image_url = discord.ui.TextInput(
        label='URL pública da imagem',
        placeholder='https://exemplo.com/imagem.png',
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        url = str(self.image_url.value).strip()
        if not url.startswith(('https://', 'http://')):
            await interaction.response.send_message('❌ Envie um link de imagem começando com https:// ou http://.', ephemeral=True)
            return
        config['queue_embed_image_url'] = url
        save_config()
        audit(interaction.guild_id, interaction.user.id, 'queue_embed_image_set_url', url[:500])
        await interaction.response.send_message('✅ Imagem salva! Ela aparecerá como miniatura nos embeds das filas novas e nas filas automáticas.', ephemeral=True)


class QueueImagePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label='🔗 Colar URL da imagem', style=discord.ButtonStyle.primary, row=0)
    async def set_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message('Apenas administradores podem alterar a imagem da fila.', ephemeral=True)
            return
        await interaction.response.send_modal(QueueImageURLModal())

    @discord.ui.button(label='🖼️ Enviar imagem aqui', style=discord.ButtonStyle.success, row=0)
    async def upload_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message('Apenas administradores podem alterar a imagem da fila.', ephemeral=True)
            return
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message('Abra o painel em um canal de texto do servidor para enviar uma imagem.', ephemeral=True)
            return
        await interaction.response.send_message(
            'Envie agora uma mensagem **neste canal** com a imagem anexada. Você tem 120 segundos. '
            'Depois de salvar o link, tentarei apagar a mensagem enviada.', ephemeral=True
        )
        def check(message):
            return (message.author.id == interaction.user.id and message.guild and
                    message.guild.id == interaction.guild_id and message.channel.id == interaction.channel_id and
                    any(a.content_type and a.content_type.startswith('image/') for a in message.attachments))
        try:
            message = await bot.wait_for('message', check=check, timeout=120)
            attachment = next(a for a in message.attachments if a.content_type and a.content_type.startswith('image/'))
            config['queue_embed_image_url'] = attachment.url
            save_config()
            audit(interaction.guild_id, interaction.user.id, 'queue_embed_image_uploaded', attachment.filename[:200])
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await interaction.followup.send('✅ Imagem recebida e salva. Ela será usada como miniatura nos embeds das filas novas e automáticas. Se o link do anexo expirar no futuro, defina novamente por URL pública.', ephemeral=True)
        except TimeoutError:
            await interaction.followup.send('⌛ Tempo esgotado. Clique novamente e envie uma imagem anexada.', ephemeral=True)

    @discord.ui.button(label='🗑️ Remover imagem', style=discord.ButtonStyle.danger, row=1)
    async def remove_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message('Apenas administradores podem alterar a imagem da fila.', ephemeral=True)
            return
        config['queue_embed_image_url'] = ''
        save_config()
        audit(interaction.guild_id, interaction.user.id, 'queue_embed_image_removed', '')
        await interaction.response.send_message('✅ Imagem removida. As filas novas serão publicadas sem miniatura.', ephemeral=True)


class AdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label='📁 Categoria Apostas', style=discord.ButtonStyle.secondary, row=0)
    async def category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Configure a categoria criando/selecionando um canal de texto e use /configurar.', ephemeral=True)

    @discord.ui.button(label='💰 Taxa Fixa', style=discord.ButtonStyle.secondary, row=0)
    async def fee(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"Taxa atual: {config.get('registration_fee')}. Para alterar, use /configurar taxa:<valor>.", ephemeral=True)

    @discord.ui.button(label='📊 Canal LOGs', style=discord.ButtonStyle.secondary, row=0)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"Canal de logs configurado: <#{config.get('log_channel_id')}>" if config.get('log_channel_id') else 'Canal de logs ainda não configurado. Use /configurar.', ephemeral=True)

    @discord.ui.button(label='⚠️ Avisos Punição', style=discord.ButtonStyle.secondary, row=0)
    async def punishment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Use /blacklist adicionar e /blacklist consultar para gerenciar punições.', ephemeral=True)

    @discord.ui.button(label='🎭 Cargo Mediador', style=discord.ButtonStyle.secondary, row=1)
    async def mediator(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Configure o cargo de mediador com /configurar mediador:@cargo.', ephemeral=True)

    @discord.ui.button(label='🛡️ Cargos SS/Analista', style=discord.ButtonStyle.secondary, row=1)
    async def analyst(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Defina os cargos permitidos usando as opções de /configurar.', ephemeral=True)

    @discord.ui.button(label='🪙 Moedas Vitória', style=discord.ButtonStyle.secondary, row=1)
    async def coins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('A recompensa por vitória está preparada para configuração na próxima etapa.', ephemeral=True)

    @discord.ui.button(label='🔄 Modo Fila', style=discord.ButtonStyle.secondary, row=1)
    async def queue_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Modo atual: fila em loop (rodízio).', ephemeral=True)

    @discord.ui.button(label='🖼️ Imagem da Fila', style=discord.ButtonStyle.primary, row=2)
    async def queue_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message('Apenas administradores podem alterar a imagem da fila.', ephemeral=True)
            return
        current = config.get('queue_embed_image_url', '')
        embed = discord.Embed(title='🖼️ Imagem do embed da fila', description=(
            'Escolha **Colar URL da imagem** ou **Enviar imagem aqui**. A imagem aparecerá como miniatura '
            'nos embeds das filas novas e nas filas automáticas.\n\n'
            f'**Configuração atual:** {current[:800] if current else "Nenhuma imagem configurada"}'
        ), color=accent())
        await interaction.response.send_message(embed=embed, view=QueueImagePanel(), ephemeral=True)

    @discord.ui.button(label='💳 Aviso Pagamento', style=discord.ButtonStyle.secondary, row=2)
    async def payment_notice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"Aviso atual: {config.get('payment_notice')}", ephemeral=True)

    @discord.ui.button(label='👑 Cargo Dono', style=discord.ButtonStyle.secondary, row=3)
    async def owner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Configure o cargo de dono com /configurar dono:@cargo.', ephemeral=True)

    @discord.ui.button(label='🧰 Cargo Suporte', style=discord.ButtonStyle.secondary, row=3)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Configure o cargo de suporte com /configurar suporte:@cargo.', ephemeral=True)

    @discord.ui.button(label='🎨 Alterar Aparência', style=discord.ButtonStyle.secondary, row=3)
    async def appearance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('A cor de destaque atual é #{:06X}. Use /configurar cor para alterar.'.format(int(config['accent_color'])), ephemeral=True)

    @discord.ui.button(label='🔴 Desativar/Ativar Apostas', style=discord.ButtonStyle.danger, row=3)
    async def toggle_bets(self, interaction: discord.Interaction, button: discord.ui.Button):
        config['bets_enabled'] = not config.get('bets_enabled', True)
        save_config()
        audit(interaction.guild_id, interaction.user.id, 'toggle_bets', str(config['bets_enabled']))
        await interaction.response.send_message(f"Apostas {'ativadas' if config['bets_enabled'] else 'desativadas'}.", ephemeral=True)

    @discord.ui.button(label='❌ Fechar Painel', style=discord.ButtonStyle.danger, row=4)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content='Painel fechado.', embed=None, view=None)


@bot.tree.command(name='fila', description='Publica uma fila aberta para jogadores entrarem sem escolher oponente.')
@app_commands.describe(formato='Ex.: 2v2', plataforma='Mobile ou Emulador', modo='Ex.: UMP e XM8', valor='Valor da aposta, ex.: R$ 0,65')
async def fila(interaction: discord.Interaction, formato: str, plataforma: str, modo: str, valor: str):
    if not config.get('bets_enabled', True):
        await interaction.response.send_message('As filas estão temporariamente desativadas.', ephemeral=True); return
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message('Use este comando em um canal de texto do servidor.', ephemeral=True); return
    with db() as conn:
        cur = conn.execute('INSERT INTO matches(guild_id, channel_id, host_id, opponent_id, format, platform, mode, stake, status, confirmed) VALUES (?, ?, 0, NULL, ?, ?, ?, ?, ?, ?)', (interaction.guild_id, interaction.channel_id, formato, plataforma, modo, valor, 'waiting', '[]'))
        match_id = cur.lastrowid
    # Campos na mesma linha deixam o embed mais baixo, sem alterar o tamanho da miniatura.
    embed = discord.Embed(title=f'{formato} {plataforma}', color=accent())
    embed.add_field(name='Modo', value=modo, inline=True)
    embed.add_field(name='Valor', value=valor, inline=True)
    embed.add_field(name='Jogadores em fila', value='0/2', inline=True)
    queue_image = str(config.get('queue_embed_image_url', '') or '').strip()
    if queue_image.startswith(('https://', 'http://')):
        embed.set_thumbnail(url=queue_image)
    await interaction.response.send_message('✅ Fila publicada neste canal.', ephemeral=True)
    msg = await interaction.channel.send(embed=embed, view=QueueView(match_id))
    with db() as conn: conn.execute('UPDATE matches SET message_id=? WHERE id=?', (msg.id, match_id))
    audit(interaction.guild_id, interaction.user.id, 'bet_opened', f'match_id={match_id}; public_channel={interaction.channel_id}; formato={formato}; valor={valor}')

@tasks.loop(minutes=30)
async def queue_refresh_loop():
    """Atualiza a mensagem da fila existente e avisa @everyone a cada 30 minutos."""
    if not bot.is_ready():
        return

    # Uma fila aberta por servidor: reutiliza a mensagem já publicada, sem criar outra.
    with db() as conn:
        guild_ids = [r['guild_id'] for r in conn.execute(
            "SELECT DISTINCT guild_id FROM matches WHERE status='waiting' AND message_id IS NOT NULL"
        ).fetchall()]

    for guild_id in guild_ids:
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM matches WHERE guild_id=? AND status='waiting' AND message_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (guild_id,)
                ).fetchone()
            if not row:
                continue

            channel = bot.get_channel(int(row['channel_id']))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(row['channel_id']))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                message = await channel.fetch_message(int(row['message_id']))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

            # Atualiza a própria mensagem da fila, mantendo o mesmo ID e os botões.
            view = QueueView(int(row['id']))
            embed = view.make_embed(row)
            embed.set_footer(text='Fila atualizada automaticamente • Aguardando jogadores')
            await message.edit(embed=embed, view=view)

            # A menção é enviada em mensagem separada porque editar uma mensagem não dispara novo ping.
            perms = channel.permissions_for(channel.guild.me)
            if perms.send_messages and perms.mention_everyone:
                await channel.send(
                    content='@everyone 🔔 **A fila está aberta!** Entre na fila neste canal e clique em **Entrar na Fila**.',
                    allowed_mentions=discord.AllowedMentions(everyone=True)
                )
            audit(int(guild_id), None, 'queue_auto_refresh', f'match_id={row["id"]}; channel_id={channel.id}; everyone_ping={bool(perms.send_messages and perms.mention_everyone)}')
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
            print(f'[fila automática] Não foi possível atualizar/avisar no servidor {guild_id}: {exc}')

@queue_refresh_loop.before_loop
async def before_queue_refresh_loop():
    await bot.wait_until_ready()

class BlacklistLookupModal(discord.ui.Modal, title='Verificação de ID na Blacklist'):
    player_id = discord.ui.TextInput(
        label='Digite o ID que você deseja verificar:',
        placeholder='Ex: 123456789',
        required=True,
        min_length=1,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction):
        player_id = str(self.player_id.value).strip()
        if not player_id.isdigit():
            await interaction.response.send_message('Informe um ID numérico válido.', ephemeral=True)
            return
        with db() as conn:
            row = conn.execute('SELECT player_id, reason FROM blacklist WHERE player_id=?', (player_id,)).fetchone()
        embed = discord.Embed(title='🚫 BLACKLIST', color=accent())
        if row:
            embed.description = f"⛔ Jogador está na blacklist.\n**ID:** `{row['player_id']}`\n**Motivo:** {row['reason']}"
        else:
            embed.description = '✅ Nenhum jogador está na blacklist.'
        embed.set_footer(text=f"{config['brand_name']} BOT • Consulta")
        audit(interaction.guild_id, interaction.user.id, 'blacklist_lookup_modal', f'id={player_id}; found={bool(row)}')
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BlacklistPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='🔍 Verificar ID', style=discord.ButtonStyle.secondary, custom_id='colombia:blacklist:verify')
    async def verify_id(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BlacklistLookupModal())


@bot.tree.command(name='publicar_blacklist', description='Publica o painel de verificação de ID na blacklist.')
async def publicar_blacklist(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem publicar este painel.', ephemeral=True)
        return
    embed = discord.Embed(
        title='Blacklist',
        description='Clique no botão abaixo para verificar um ID na blacklist.',
        color=accent(),
    )
    await interaction.channel.send(embed=embed, view=BlacklistPanelView())
    audit(interaction.guild_id, interaction.user.id, 'blacklist_panel_publish', f'channel={interaction.channel_id}')
    await interaction.response.send_message('✅ Painel de blacklist publicado neste canal.', ephemeral=True)

blacklist_group = app_commands.Group(name='blacklist', description='Consulta e administra a blacklist de jogadores.')

@blacklist_group.command(name='consultar', description='Consulta um ID na blacklist.')
@app_commands.describe(player_id='ID do jogador')
async def blacklist_consultar(interaction: discord.Interaction, player_id: str):
    with db() as conn:
        row = conn.execute('SELECT * FROM blacklist WHERE player_id=?', (player_id.strip(),)).fetchone()
    embed = discord.Embed(title='🚫 BLACKLIST', color=accent())
    if row:
        embed.description = f"⛔ Jogador está na blacklist.\n**ID:** `{row['player_id']}`\n**Motivo:** {row['reason']}"
    else:
        embed.description = '✅ Nenhum jogador está na blacklist com esse ID.'
    embed.set_footer(text=f"{config['brand_name']} BOT • Consulta")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@blacklist_group.command(name='adicionar', description='Adiciona um ID à blacklist.')
@app_commands.describe(player_id='ID do jogador', motivo='Motivo da inclusão')
async def blacklist_adicionar(interaction: discord.Interaction, player_id: str, motivo: str):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem adicionar à blacklist.', ephemeral=True)
        return
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO blacklist(player_id, reason, added_by) VALUES (?, ?, ?)', (player_id.strip(), motivo, interaction.user.id))
    audit(interaction.guild_id, interaction.user.id, 'blacklist_add', f'id={player_id}; motivo={motivo}')
    await interaction.response.send_message(f'🚫 ID `{player_id}` adicionado à blacklist. Motivo: {motivo}', ephemeral=True)

@blacklist_group.command(name='remover', description='Remove um ID da blacklist.')
@app_commands.describe(player_id='ID do jogador')
async def blacklist_remover(interaction: discord.Interaction, player_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem remover da blacklist.', ephemeral=True)
        return
    with db() as conn:
        cur = conn.execute('DELETE FROM blacklist WHERE player_id=?', (player_id.strip(),))
    if cur.rowcount:
        audit(interaction.guild_id, interaction.user.id, 'blacklist_remove', f'id={player_id}')
        await interaction.response.send_message(f'✅ ID `{player_id}` removido da blacklist.', ephemeral=True)
    else:
        await interaction.response.send_message('Esse ID não estava na blacklist.', ephemeral=True)

bot.tree.add_command(blacklist_group)

@bot.tree.command(name='configurar_cargo_mediador', description='Seleciona diretamente o cargo que pode usar o painel de mediadores.')
@app_commands.describe(cargo='Selecione o cargo de mediador do servidor')
async def configurar_cargo_mediador(interaction: discord.Interaction, cargo: discord.Role):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem alterar configurações.', ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message('Use este comando dentro do servidor.', ephemeral=True)
        return
    role_map = config.setdefault('guild_mediator_role_ids', {})
    role_map[str(interaction.guild.id)] = cargo.id
    save_config()
    audit(interaction.guild_id, interaction.user.id, 'config_update', f'mediator_role_id={cargo.id} ({cargo.name}) para guild={interaction.guild.id}')
    await interaction.response.send_message(
        f'✅ Cargo de mediador configurado: {cargo.mention} (ID `{cargo.id}`).\n'
        'Agora confirme que sua conta tem exatamente esse cargo e clique novamente em **ENTRAR EM FILA**.',
        ephemeral=True
    )


@bot.tree.command(name='configurar', description='Configura taxa, PIX, cor, cargos ou canais do bot.')
@app_commands.describe(chave='Configuração a alterar', valor='Novo valor (para canais/cargos, informe o ID numérico; para cor, RRGGBB)')
@app_commands.choices(chave=[
    app_commands.Choice(name='taxa', value='registration_fee'),
    app_commands.Choice(name='pix', value='pix_key'),
    app_commands.Choice(name='tipo_pix', value='pix_type'),
    app_commands.Choice(name='qr_pix_url', value='pix_qr_image_url'),
    app_commands.Choice(name='aviso_pagamento', value='payment_notice'),
    app_commands.Choice(name='cor_hex', value='accent_color'),
    app_commands.Choice(name='cargo_dono_id', value='owner_role_id'),
    app_commands.Choice(name='cargo_mediador_id', value='mediator_role_id'),
    app_commands.Choice(name='cargo_suporte_id', value='support_role_id'),
    app_commands.Choice(name='canal_logs_id', value='log_channel_id'),
    app_commands.Choice(name='logs_filas_id', value='log_queue_channel_id'),
    app_commands.Choice(name='logs_apostas_id', value='log_bets_channel_id'),
    app_commands.Choice(name='logs_apostas_abertas_id', value='log_bets_open_channel_id'),
    app_commands.Choice(name='logs_apostas_fechadas_id', value='log_bets_closed_channel_id'),
    app_commands.Choice(name='logs_apostas_confirmadas_id', value='log_bets_confirmed_channel_id'),
    app_commands.Choice(name='logs_apostas_finalizadas_id', value='log_bets_finalized_channel_id'),
    app_commands.Choice(name='logs_partidas_id', value='log_match_channel_id'),
    app_commands.Choice(name='logs_mediadores_id', value='log_mediator_channel_id'),
    app_commands.Choice(name='logs_blacklist_id', value='log_blacklist_channel_id'),
    app_commands.Choice(name='logs_admin_id', value='log_admin_channel_id'),
    app_commands.Choice(name='canal_blacklist_id', value='blacklist_channel_id'),
    app_commands.Choice(name='categoria_apostas_id', value='bets_category_id'),
    app_commands.Choice(name='categoria_mediadores_id', value='mediator_category_id'),
])
async def configurar(interaction: discord.Interaction, chave: app_commands.Choice[str], valor: str):
    if not is_admin(interaction):
        await interaction.response.send_message('Apenas administradores podem alterar configurações.', ephemeral=True)
        return
    key = chave.value
    try:
        if key == 'accent_color':
            cleaned = valor.strip().replace('#', '').replace('0x', '')
            parsed = int(cleaned, 16)
            if not 0 <= parsed <= 0xFFFFFF:
                raise ValueError
            config[key] = parsed
        elif key == 'mediator_role_id':
            role_id = int(valor)
            if interaction.guild is None:
                await interaction.response.send_message('Use este comando dentro do servidor.', ephemeral=True)
                return
            role = await resolve_guild_role(interaction.guild, role_id)
            if role is None:
                await interaction.response.send_message('Não encontrei esse cargo neste servidor. Use o ID de um cargo existente aqui.', ephemeral=True)
                return
            config.setdefault('guild_mediator_role_ids', {})[str(interaction.guild.id)] = role_id
        elif key.endswith('_id'):
            config[key] = int(valor)
        else:
            config[key] = valor
    except ValueError:
        await interaction.response.send_message('Valor inválido. Para cor, use RRGGBB; para IDs, use apenas números.', ephemeral=True)
        return
    save_config()
    audit(interaction.guild_id, interaction.user.id, 'config_update', f'{key}={valor}')
    await interaction.response.send_message(f'✅ Configuração `{key}` atualizada.', ephemeral=True)

@bot.tree.command(name='regras', description='Publica as regras padrão das filas.')
async def regras(interaction: discord.Interaction):
    await interaction.response.send_message(embed=rules_embed())

if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('Configure DISCORD_TOKEN no arquivo .env antes de iniciar o bot.')
    bot.run(TOKEN)
