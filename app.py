# app.py
import uuid
import datetime
from flask import Flask, request, session, redirect, jsonify, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms

app = Flask(__name__)
app.secret_key = "dev-secret-change-me"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ------------ In-memory storage (demo only) ------------
users_by_name = {}  # username -> {password, user_id, registered_at}
users_by_id = {}    # user_id -> username
online = {}         # user_id -> {'sid': socket sid, 'busy': False, 'room': None, 'name': username}
calls = {}          # call_id -> {'owner': user_id, 'members': set(user_ids)}

GATE_PASSWORD = "Fikekwp!_17+919"

# ------------ Helpers ------------
def is_authed():
    return session.get("authed") is True and session.get("user_id") in users_by_id

def current_user():
    if not is_authed():
        return None
    uid = session.get("user_id")
    uname = users_by_id.get(uid)
    if not uname:
        return None
    profile = users_by_name.get(uname, {}).copy()
    profile["username"] = uname
    return profile

def new_user_id():
    # короткий читаемый ID
    return uuid.uuid4().hex[:8]

def now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

# ------------ HTTP routes ------------
@app.route("/", methods=["GET"])
def index():
    return render_template_string(TEMPLATE_HTML, gate_passed=session.get("gate_passed", False), authed=is_authed())

@app.post("/gate")
def gate():
    data = request.json or {}
    if data.get("password") == GATE_PASSWORD:
        session["gate_passed"] = True
        return jsonify(ok=True)
    return jsonify(ok=False, error="Неверный пароль"), 403

@app.post("/register")
def register():
    if not session.get("gate_passed"):
        return jsonify(ok=False, error="Сначала введите пароль доступа"), 403
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify(ok=False, error="Укажите имя и пароль"), 400
    if username in users_by_name:
        return jsonify(ok=False, error="Имя уже занято"), 409
    uid = new_user_id()
    users_by_name[username] = {
        "password": password,
        "user_id": uid,
        "registered_at": now_iso()
    }
    users_by_id[uid] = username
    session["authed"] = True
    session["user_id"] = uid
    return jsonify(ok=True, user_id=uid, username=username)

@app.post("/login")
def login():
    if not session.get("gate_passed"):
        return jsonify(ok=False, error="Сначала введите пароль доступа"), 403
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    u = users_by_name.get(username)
    if not u or u["password"] != password:
        return jsonify(ok=False, error="Неверные имя или пароль"), 403
    session["authed"] = True
    session["user_id"] = u["user_id"]
    return jsonify(ok=True, user_id=u["user_id"], username=username)

@app.get("/me")
def me():
    if not is_authed():
        return jsonify(ok=False), 401
    u = current_user()
    return jsonify(ok=True, profile={
        "username": u["username"],
        "user_id": u["user_id"],
        "registered_at": u["registered_at"],
        "online": u["user_id"] in online
    })

@app.get("/who/<user_id>")
def who(user_id):
    uname = users_by_id.get(user_id)
    if not uname:
        return jsonify(ok=False, error="Пользователь не найден"), 404
    return jsonify(ok=True, profile={
        "username": uname,
        "user_id": user_id,
        "registered_at": users_by_name[uname]["registered_at"],
        "online": user_id in online
    })

@app.get("/logout")
def logout():
    uid = session.get("user_id")
    session.clear()
    if uid and uid in online:
        # пометим как оффлайн и уведомим
        info = online.pop(uid, None)
        if info:
            socketio.emit("presence:update", {"user_id": uid, "online": False}, broadcast=True)
    return redirect("/")

# ------------ Socket.IO (signaling / presence) ------------
@socketio.on("connect")
def sock_connect():
    # подключение сокета ещё не означает авторизацию, ждём 'presence:online'
    emit("connected", {"ok": True})

@socketio.on("disconnect")
def sock_disconnect():
    # если этот сокет принадлежал юзеру — снимем онлайн/занят
    for uid, info in list(online.items()):
        if info.get("sid") == request.sid:
            # если был в активном звонке — известим участников
            room = info.get("room")
            online.pop(uid, None)
            socketio.emit("presence:update", {"user_id": uid, "online": False}, broadcast=True)
            if room:
                # уведомим комнату, что участник вышел
                emit("call:ended", {"user_id": uid, "reason": "left"}, to=room, include_self=False)
            break

@socketio.on("presence:online")
def presence_online(data):
    # data: {user_id, username}
    uid = data.get("user_id")
    uname = data.get("username")
    if not uid or users_by_id.get(uid) != uname:
        emit("presence:error", {"error": "Неавторизовано"})
        return
    # отметим онлайн
    online[uid] = {"sid": request.sid, "busy": False, "room": None, "name": uname}
    socketio.emit("presence:update", {"user_id": uid, "online": True}, broadcast=True)

@socketio.on("presence:list")
def presence_list():
    emit("presence:list", {
        "online": [{ "user_id": uid, "username": info["name"], "busy": info["busy"] } for uid, info in online.items()]
    })

# ---- Calling flow (mesh WebRTC, one room per call) ----
@socketio.on("call:start")
def call_start(data):
    """Initiate a call to one or more IDs."""
    caller = data.get("from")
    targets = set([t for t in data.get("to", []) if t and t in users_by_id])
    if not caller or caller not in online:
        emit("call:error", {"error": "Вы не онлайн"})
        return
    # нельзя звонить оффлайн
    offline_targets = [t for t in targets if t not in online]
    if offline_targets:
        emit("call:error", {"error": "Некоторые адресаты оффлайн", "offline": offline_targets})
        return
    # проверим занятость
    busy_targets = [t for t in targets if online[t]["busy"]]
    if online[caller]["busy"]:
        emit("call:error", {"error": "Вы уже в звонке"})
        return
    if busy_targets:
        emit("call:error", {"error": "Адресат(ы) занят(ы)", "busy": busy_targets})
        return

    call_id = uuid.uuid4().hex[:10]
    room = f"call:{call_id}"
    calls[call_id] = {"owner": caller, "members": set([caller])}
    online[caller]["busy"] = True
    online[caller]["room"] = room
    join_room(room)

    # Пригласим адресатов
    for t in targets:
        emit("call:incoming", {
            "call_id": call_id,
            "room": room,
            "from": caller
        }, to=online[t]["sid"])

    emit("call:created", {"call_id": call_id, "room": room})

@socketio.on("call:invite")
def call_invite(data):
    """Invite more participants by ID to existing call."""
    call_id = data.get("call_id")
    inviter = data.get("from")
    targets = set([t for t in data.get("to", []) if t and t in users_by_id])
    call = calls.get(call_id)
    if not call or inviter not in call["members"]:
        emit("call:error", {"error": "Звонок не найден или нет прав"})
        return
    room = f"call:{call_id}"
    for t in targets:
        if t not in online:
            emit("call:note", {"note": f"Пользователь {t} оффлайн"})
            continue
        if online[t]["busy"]:
            emit("call:note", {"note": f"Пользователь {t} занят"})
            continue
        emit("call:incoming", {"call_id": call_id, "room": room, "from": inviter}, to=online[t]["sid"])

@socketio.on("call:accept")
def call_accept(data):
    call_id = data.get("call_id")
    user_id = data.get("user_id")
    call = calls.get(call_id)
    if not call:
        emit("call:error", {"error": "Звонок не найден"})
        return
    room = f"call:{call_id}"
    join_room(room)
    call["members"].add(user_id)
    online[user_id]["busy"] = True
    online[user_id]["room"] = room
    emit("call:joined", {"user_id": user_id}, to=room, include_self=False)

@socketio.on("call:decline")
def call_decline(data):
    caller = data.get("from")
    call_id = data.get("call_id")
    # уведомим инициатора/комнату
    call = calls.get(call_id)
    if call:
        room = f"call:{call_id}"
        emit("call:declined", {"user_id": caller}, to=room)

@socketio.on("call:end")
def call_end(data):
    call_id = data.get("call_id")
    user_id = data.get("user_id")
    call = calls.get(call_id)
    if not call:
        return
    room = f"call:{call_id}"
    leave_room(room)
    if user_id in call["members"]:
        call["members"].remove(user_id)
    if user_id in online:
        online[user_id]["busy"] = False
        online[user_id]["room"] = None
    emit("call:ended", {"user_id": user_id, "reason": "hangup"}, to=room, include_self=False)
    # если никого не осталось — закрываем
    if len(call["members"]) == 0:
        calls.pop(call_id, None)

# --- WebRTC signaling passthrough (mesh: peer <-> peer via room) ---
@socketio.on("webrtc:offer")
def webrtc_offer(data):
    # data: {call_id, from, to(optional), sdp}
    call_id = data.get("call_id")
    sender = data.get("from")
    sdp = data.get("sdp")
    call = calls.get(call_id)
    if not call:
        emit("call:error", {"error": "Нет разговора"})
        return
    room = f"call:{call_id}"
    # Рассылаем всем, кроме отправителя (каждый получатель создаст ответ)
    emit("webrtc:offer", {"from": sender, "sdp": sdp}, to=room, include_self=False)

@socketio.on("webrtc:answer")
def webrtc_answer(data):
    call_id = data.get("call_id")
    sender = data.get("from")
    sdp = data.get("sdp")
    call = calls.get(call_id)
    if not call:
        return
    room = f"call:{call_id}"
    emit("webrtc:answer", {"from": sender, "sdp": sdp}, to=room, include_self=False)

@socketio.on("webrtc:candidate")
def webrtc_candidate(data):
    call_id = data.get("call_id")
    sender = data.get("from")
    candidate = data.get("candidate")
    call = calls.get(call_id)
    if not call:
        return
    room = f"call:{call_id}"
    emit("webrtc:candidate", {"from": sender, "candidate": candidate}, to=room, include_self=False)

# ------------ HTML / CSS / JS (single file) ------------
TEMPLATE_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no" />
  <title>Видеозвонки (демо)</title>
  <style>
    :root { --bg:#0b1020; --card:#151b2f; --ink:#e6ecff; --muted:#a9b3d1; --accent:#5b8cff; --ok:#2ecc71; --warn:#f39c12; --err:#e74c3c; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; background: linear-gradient(180deg,#0b1020,#0e1326 40%,#0b1020); color:var(--ink);}
    .wrap { max-width: 860px; margin: 0 auto; padding: 16px; }
    .card { background:var(--card); border:1px solid #1f2744; border-radius:20px; padding:16px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }
    h1 { font-size: 20px; margin: 0 0 12px 0; }
    input, button { width:100%; padding:14px; border-radius:14px; border:1px solid #2a3358; background:#0e1430; color:var(--ink); outline: none; }
    input::placeholder { color: #6f7aa8; }
    button { background: linear-gradient(180deg,#628dff,#4c75ff); border:none; font-weight: 700; letter-spacing:.2px; cursor: pointer; }
    button.ghost { background: #0e1430; border:1px solid #2a3358; }
    .row { display:flex; gap:10px; }
    .row > * { flex:1; }
    .mt8{margin-top:8px}.mt12{margin-top:12px}.mt16{margin-top:16px}.mt24{margin-top:24px}
    .center { text-align:center }
    .muted { color:var(--muted); font-size: 13px; }
    .tag { display:inline-block; padding:4px 10px; border-radius:999px; border:1px solid #2a3358; background:#0e1430; font-size:12px; color:#c7d2ff;}
    .list { display:flex; gap:8px; flex-wrap: wrap; }
    .status { font-size: 12px; padding:2px 8px; border-radius:999px; }
    .ok{background:#123; color:#7cffc3; border:1px solid #235;}
    .warn{background:#231; color:#ffd37c; border:1px solid #542;}
    .err{background:#311; color:#ff9b9b; border:1px solid #633;}
    video { width: 100%; border-radius: 16px; background: #000; }
    .grid { display:grid; grid-template-columns: 1fr; gap:10px; }
    @media(min-width:720px){ .grid { grid-template-columns: 1fr 1fr; } }
    .pillbar{display:flex; gap:8px; flex-wrap:wrap}
    .pillbar button{flex:1}
    .toolbar{display:flex; gap:10px; align-items:center; flex-wrap:wrap}
    .toolbar button{flex:1}
    .tiny{font-size:12px}
    .bubble{background:#0f1533;border:1px solid #27315c;padding:10px;border-radius:12px}
    .log{max-height:140px; overflow:auto; font-size:12px; line-height:1.45; background:#0c122b; border:1px solid #1a2248; border-radius:12px; padding:8px}
    .kb{font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; background:#0a1330; padding:2px 6px; border-radius:6px; border:1px solid #1f2748}
  </style>
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js" crossorigin="anonymous"></script>
</head>
<body>
  <div class="wrap">
    <div class="card" id="screen-gate" style="display:none">
      <h1>Доступ</h1>
      <div class="muted">Введите пароль доступа, чтобы продолжить.</div>
      <input id="gate-pass" class="mt12" type="password" placeholder="Пароль доступа" />
      <button class="mt12" onclick="submitGate()">Войти</button>
      <div id="gate-err" class="mt12 err status" style="display:none"></div>
    </div>

    <div class="card" id="screen-auth" style="display:none">
      <h1>Регистрация / Вход</h1>
      <div class="row mt12">
        <input id="reg-name" placeholder="Имя (логин)" />
        <input id="reg-pass" type="password" placeholder="Пароль" />
      </div>
      <div class="row mt12">
        <button onclick="register()">Зарегистрироваться</button>
        <button class="ghost" onclick="login()">Войти</button>
      </div>
      <div id="auth-err" class="mt12 err status" style="display:none"></div>
      <div class="mt12 muted tiny">Демо-режим: данные хранятся в памяти и пропадут при перезапуске.</div>
    </div>

    <div class="card" id="screen-app" style="display:none">
      <div class="toolbar">
        <div class="tag">ID: <span id="me-id">—</span></div>
        <div class="tag">Имя: <span id="me-name">—</span></div>
        <div class="tag">Регистрация: <span id="me-reg">—</span></div>
        <div class="status ok" id="me-online">оффлайн</div>
        <button class="ghost" onclick="doLogout()">Выйти</button>
      </div>

      <div class="mt16 grid">
        <div>
          <h1>Звонок</h1>
          <div class="bubble">
            <div class="row">
              <input id="call-id" placeholder="ID пользователя" />
              <button onclick="startCall()">Позвонить</button>
            </div>
            <div class="row mt8">
              <input id="invite-id" placeholder="Добавить участника по ID" />
              <button class="ghost" onclick="invite()">Добавить</button>
            </div>
            <div class="pillbar mt12">
              <button class="ghost" id="btn-mic" onclick="toggleMic()">Микрофон: вкл</button>
              <button class="ghost" id="btn-cam" onclick="toggleCam()">Камера: вкл</button>
              <button class="ghost" id="btn-av" onclick="toggleAV()">Аудио/Видео</button>
              <button class="ghost" onclick="hangup()">Сброс</button>
            </div>
            <div class="mt12">
              <div class="status warn tiny" id="call-status">Нет звонка</div>
            </div>
            <div class="mt12 log" id="log"></div>
          </div>

          <h1 class="mt24">Поиск по ID</h1>
          <div class="bubble">
            <div class="row">
              <input id="lookup-id" placeholder="Например: a1b2c3d4" />
              <button class="ghost" onclick="lookup()">Найти</button>
            </div>
            <div class="mt8" id="lookup-res"></div>
          </div>

          <h1 class="mt24">Кто онлайн</h1>
          <div class="bubble">
            <div id="online-list" class="list"></div>
          </div>
        </div>

        <div>
          <h1>Видео</h1>
          <div class="bubble">
            <div class="muted tiny">Ваше видео</div>
            <video id="localVideo" playsinline autoplay muted></video>
            <div class="muted tiny mt12">Участники</div>
            <div id="remotes"></div>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
const state = {
  gatePassed: {{ 'true' if gate_passed else 'false' }},
  authed: {{ 'true' if authed else 'false' }},
  me: null,
  socket: null,
  pcMap: new Map(), // remoteUserId -> RTCPeerConnection
  stream: null,
  callId: null,
  room: null,
  micOn: true,
  camOn: true,
  inCall: false,
};

function $(id){ return document.getElementById(id); }
function show(id, yes){ $(id).style.display = yes ? '' : 'none'; }
function log(msg){ const el = $("log"); const at = new Date().toLocaleTimeString(); el.innerHTML = `[${at}] ${msg}<br>` + el.innerHTML; }
function setStatus(text, cls="warn"){ const el=$("call-status"); el.className="status "+cls+" tiny"; el.textContent=text; }
function tag(txt){ const s=document.createElement('span'); s.className='tag'; s.textContent=txt; return s; }

function renderScreen(){
  show("screen-gate", !state.gatePassed);
  show("screen-auth", state.gatePassed && !state.authed);
  show("screen-app", state.gatePassed && state.authed);
}

async function submitGate(){
  const password = $("gate-pass").value;
  const r = await fetch("/gate", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({password})});
  if(r.ok){ state.gatePassed=true; renderScreen(); initAfterAuthCheck(); }
  else{ const j=await r.json(); $("gate-err").style.display=''; $("gate-err").textContent=j.error||"Ошибка"; }
}

async function register(){
  const username = $("reg-name").value.trim();
  const password = $("reg-pass").value.trim();
  const r = await fetch("/register",{method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username,password})});
  const j = await r.json();
  if(j.ok){ state.authed=true; renderScreen(); initAfterAuthCheck(); }
  else{ $("auth-err").style.display=''; $("auth-err").textContent=j.error||"Ошибка"; }
}

async function login(){
  const username = $("reg-name").value.trim();
  const password = $("reg-pass").value.trim();
  const r = await fetch("/login",{method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username,password})});
  const j = await r.json();
  if(j.ok){ state.authed=true; renderScreen(); initAfterAuthCheck(); }
  else{ $("auth-err").style.display=''; $("auth-err").textContent=j.error||"Ошибка"; }
}

async function initAfterAuthCheck(){
  if(!(state.gatePassed && state.authed)) return;
  // загрузим профиль
  const me = await fetch("/me").then(r=>r.json());
  if(!me.ok) return;
  state.me = me.profile;
  $("me-id").textContent = state.me.user_id;
  $("me-name").textContent = state.me.username;
  $("me-reg").textContent = state.me.registered_at;
  $("me-online").textContent = "онлайн";
  // медиа
  await setupMedia();
  // сокет
  setupSocket();
}

async function setupMedia(){
  try{
    const stream = await navigator.mediaDevices.getUserMedia({audio:true, video:{width:{ideal:1280}, height:{ideal:720}}});
    state.stream = stream;
    $("localVideo").srcObject = stream;
  }catch(e){
    log("Не удалось получить доступ к камере/микрофону: " + e.message);
  }
}

function setupSocket(){
  state.socket = io("/", {transports:["websocket"]});
  state.socket.on("connect", ()=>{
    state.socket.emit("presence:online", {user_id: state.me.user_id, username: state.me.username});
    state.socket.emit("presence:list");
  });
  state.socket.on("presence:update", (p)=>{
    renderPresence();
  });
  state.socket.on("presence:list", (p)=>{
    renderPresence(p.online);
  });

  // Call signaling
  state.socket.on("call:incoming", ({call_id, room, from})=>{
    if(state.inCall){
      // уже заняты
      state.socket.emit("call:decline", {call_id, from: state.me.user_id});
      log("Входящий вызов от " + from + " — занято");
      return;
    }
    if(confirm("Входящий звонок от ID " + from + ". Принять?")){
      state.callId = call_id; state.room = room; state.inCall = true;
      state.socket.emit("call:accept", {call_id, user_id: state.me.user_id});
      setStatus("Соединение...", "warn");
      startMeshAsJoiner();
    }else{
      state.socket.emit("call:decline", {call_id, from: state.me.user_id});
      log("Вы отклонили вызов");
      setStatus("Вызов отклонён", "err");
    }
  });

  state.socket.on("call:created", ({call_id, room})=>{
    state.callId = call_id; state.room = room; state.inCall = true;
    setStatus("Исходящий... Ожидаем ответ", "warn");
    startMeshAsOwner();
  });

  state.socket.on("call:joined", ({user_id})=>{
    log("Пользователь подключился: " + user_id);
    // владелец/участники создают оффер к новому участнику
    createPeer(user_id, true);
  });

  state.socket.on("call:declined", ({user_id})=>{
    log("Пользователь " + user_id + " отклонил вызов");
  });

  state.socket.on("call:ended", ({user_id, reason})=>{
    log("Пользователь " + user_id + " вышел ("+reason+")");
    removePeer(user_id);
    if(state.pcMap.size===0){
      teardownCallUI();
      setStatus("Звонок завершён", "warn");
    }
  });

  // WebRTC SDP/ICE
  state.socket.on("webrtc:offer", async ({from, sdp})=>{
    // создаём PC если нет, отвечаем
    const pc = createPeer(from, false);
    await pc.setRemoteDescription(new RTCSessionDescription(sdp));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    state.socket.emit("webrtc:answer", {call_id: state.callId, from: state.me.user_id, sdp: pc.localDescription});
  });

  state.socket.on("webrtc:answer", async ({from, sdp})=>{
    const pc = state.pcMap.get(from);
    if(!pc) return;
    await pc.setRemoteDescription(new RTCSessionDescription(sdp));
  });

  state.socket.on("webrtc:candidate", async ({from, candidate})=>{
    const pc = state.pcMap.get(from);
    if(!pc || !candidate) return;
    try{ await pc.addIceCandidate(new RTCIceCandidate(candidate)); }catch(e){ log("ICE error: "+e.message); }
  });
}

function renderPresence(list){
  if(!list){ state.socket.emit("presence:list"); return; }
  const box = $("online-list"); box.innerHTML = "";
  list.forEach(u=>{
    const el = document.createElement('div');
    el.className = 'tag';
    el.textContent = `${u.username} (${u.user_id}) ${u.busy?'• занят':'• свободен'}`;
    box.appendChild(el);
  });
}

async function lookup(){
  const id = $("lookup-id").value.trim();
  if(!id) return;
  const r = await fetch("/who/"+encodeURIComponent(id));
  const j = await r.json();
  const out = $("lookup-res");
  if(j.ok){
    out.innerHTML = "";
    out.append(tag("Имя: "+j.profile.username));
    out.append(" ");
    out.append(tag("ID: "+j.profile.user_id));
    out.append(" ");
    out.append(tag(j.profile.online ? "онлайн":"оффлайн"));
  }else{
    out.innerHTML = '<span class="status err">Не найден</span>';
  }
}

async function startCall(){
  const id = $("call-id").value.trim();
  if(!id) return;
  if(state.inCall){ alert("Вы уже в звонке"); return; }
  state.socket.emit("call:start", {from: state.me.user_id, to: [id]});
}

function invite(){
  if(!state.inCall){ alert("Сначала начните звонок"); return; }
  const id = $("invite-id").value.trim();
  if(!id) return;
  state.socket.emit("call:invite", {call_id: state.callId, from: state.me.user_id, to: [id]});
  log("Приглашение отправлено: " + id);
}

function pcConfig(){
  // Для продакшена используйте свой STUN/TURN
  return { iceServers: [{urls:"stun:stun.l.google.com:19302"}] };
}

function createPeer(remoteUserId, isInitiator){
  if(state.pcMap.has(remoteUserId)) return state.pcMap.get(remoteUserId);
  const pc = new RTCPeerConnection(pcConfig());
  // локальные треки
  if(state.stream){
    state.stream.getTracks().forEach(t=>pc.addTrack(t, state.stream));
  }
  // входящие
  pc.ontrack = (ev)=>{
    let v = document.getElementById("remote-"+remoteUserId);
    if(!v){
      v = document.createElement("video");
      v.id = "remote-"+remoteUserId;
      v.autoplay = true; v.playsInline = true; v.controls = false;
      $("remotes").appendChild(v);
    }
    v.srcObject = ev.streams[0];
  };
  pc.onicecandidate = (ev)=>{
    if(ev.candidate){
      state.socket.emit("webrtc:candidate", {call_id: state.callId, from: state.me.user_id, candidate: ev.candidate});
    }
  };
  pc.onconnectionstatechange = ()=>{
    if(pc.connectionState === "failed" || pc.connectionState === "disconnected"){
      log("Проблемы связи с "+remoteUserId+" ("+pc.connectionState+")");
    }
  };
  state.pcMap.set(remoteUserId, pc);

  // если инициатор — создаём оффер
  (async ()=>{
    if(isInitiator){
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      state.socket.emit("webrtc:offer", {call_id: state.callId, from: state.me.user_id, sdp: pc.localDescription});
      setStatus("Соединение...", "warn");
    }
  })();

  return pc;
}

function removePeer(remoteUserId){
  const pc = state.pcMap.get(remoteUserId);
  if(pc){ try{ pc.close(); }catch{}; state.pcMap.delete(remoteUserId); }
  const v = $("remote-"+remoteUserId);
  if(v){ v.srcObject = null; v.remove(); }
}

function teardownCallUI(){
  // закрыть всех
  for(const [uid, pc] of state.pcMap.entries()){
    try{ pc.close(); }catch{}
  }
  state.pcMap.clear();
  state.callId = null;
  state.room = null;
  state.inCall = false;
  setStatus("Нет звонка","warn");
}

function startMeshAsOwner(){
  // владелец ждёт присоединений и рассылает офферы при "call:joined" (сделано)
  setStatus("Звонок установлен (ожидаем участников)","ok");
  log("Создан звонок: " + state.callId);
}

function startMeshAsJoiner(){
  // как только зайдём — владельцы/участники пришлют офферы
  log("Вы присоединились к звонку: " + state.callId);
}

function toggleMic(){
  state.micOn = !state.micOn;
  if(state.stream){
    state.stream.getAudioTracks().forEach(t=>t.enabled = state.micOn);
  }
  $("btn-mic").textContent = "Микрофон: " + (state.micOn ? "вкл":"выкл");
}

function toggleCam(){
  state.camOn = !state.camOn;
  if(state.stream){
    state.stream.getVideoTracks().forEach(t=>t.enabled = state.camOn);
  }
  $("btn-cam").textContent = "Камера: " + (state.camOn ? "вкл":"выкл");
}

function toggleAV(){
  state.micOn = !state.micOn; state.camOn = !state.camOn;
  if(state.stream){
    state.stream.getAudioTracks().forEach(t=>t.enabled = state.micOn);
    state.stream.getVideoTracks().forEach(t=>t.enabled = state.camOn);
  }
  $("btn-mic").textContent = "Микрофон: " + (state.micOn ? "вкл":"выкл");
  $("btn-cam").textContent = "Камера: " + (state.camOn ? "вкл":"выкл");
}

function hangup(){
  if(!state.inCall) return;
  state.socket.emit("call:end", {call_id: state.callId, user_id: state.me.user_id});
  teardownCallUI();
  log("Вы завершили звонок");
}

async function doLogout(){
  await fetch("/logout");
  location.reload();
}

// первичная инициализация
(function boot(){
  renderScreen();
  if(state.gatePassed && state.authed){
    initAfterAuthCheck();
  }else if(state.gatePassed && !state.authed){
    // показать форму входа
  }else{
    // показать гейт
  }
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Open http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=5000)
