import os,json,hmac,hashlib,secrets,time,base64
from datetime import datetime,timezone
from typing import Optional
import httpx
from fastapi import FastAPI,HTTPException,Request,WebSocket,WebSocketDisconnect
from fastapi.responses import HTMLResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
try:
 from pymongo import MongoClient, DESCENDING
except: MongoClient=None; DESCENDING=-1
app=FastAPI(title="Benyagram");app.mount("/static",StaticFiles(directory="static"),name="static")
MONGO=os.getenv("MONGODB_URI","");GROQ=os.getenv("GROQ_API_KEY","");OW=os.getenv("OPENWEATHER_API_KEY","");SECRET=os.getenv("APP_SECRET_KEY","please-set-a-long-secret-in-render")
client=None;users={};msgs=[];sockets=set()
def db():
 global client
 if not MONGO or MongoClient is None:return None
 try:
  if client is None:client=MongoClient(MONGO,serverSelectionTimeoutMS=2500)
  return client["benyagram"]
 except:return None
def now():return datetime.now(timezone.utc).isoformat()
def phash(p,s=None):
 s=s or secrets.token_hex(16);return s+"$"+hashlib.pbkdf2_hmac("sha256",p.encode(),s.encode(),160000).hex()
def verify(p,h):
 try:s,d=h.split("$",1);return hmac.compare_digest(phash(p,s).split("$",1)[1],d)
 except:return False
def token(u):
 body=base64.urlsafe_b64encode(json.dumps({"u":u,"e":time.time()+2592000}).encode()).decode().rstrip("=")
 return body+"."+hmac.new(SECRET.encode(),body.encode(),hashlib.sha256).hexdigest()
def who(t):
 try:
  b,s=t.rsplit(".",1)
  if not hmac.compare_digest(s,hmac.new(SECRET.encode(),b.encode(),hashlib.sha256).hexdigest()):return None
  d=json.loads(base64.urlsafe_b64decode(b+"="*(-len(b)%4)))
  return d["u"] if d["e"]>time.time() else None
 except:return None
def user(u):
 d=db()
 if d is not None:
  try:return d.users.find_one({"username_key":u.lower()})
  except:pass
 return users.get(u.lower())
def allmsgs():
 d=db()
 if d is not None:
  try:return list(d.messages.find({"room":"general"},{"_id":0}).sort("created_at",DESCENDING).limit(150))[::-1]
  except:pass
 return msgs[-150:]
def save(m):
 d=db()
 if d is not None:
  try:d.messages.insert_one(m);return
  except:pass
 msgs.append(m)
def update(mid,updates,owner=None):
 d=db()
 if d is not None:
  try:
   q={"id":mid}
   if owner is not None:q["username"]=owner
   d.messages.update_one(q,{"$set":updates});return d.messages.find_one({"id":mid},{"_id":0})
  except:pass
 for m in msgs:
  if m["id"]==mid and (owner is None or m["username"]==owner):m.update(updates);return m
 return None
def clean(m):return {k:m.get(k) for k in ["id","username","display_name","text","created_at","edited_at","deleted","pinned","reply_to","kind"] if k in m}
def auth(req):
 u=who(req.cookies.get("benyagram_session",""))
 if not u:raise HTTPException(401,"Войдите в аккаунт")
 return u
async def broadcast(payload):
 for ws in list(sockets):
  try:await ws.send_json(payload)
  except:sockets.discard(ws)
class Register(BaseModel):
 username:str=Field(min_length=3,max_length=24);password:str=Field(min_length=8,max_length=128);display_name:str=Field(min_length=1,max_length=40)
class Login(BaseModel):username:str;password:str
class Message(BaseModel):text:str=Field(min_length=1,max_length=4000);reply_to:Optional[str]=None
class Edit(BaseModel):text:str=Field(min_length=1,max_length=4000)
@app.get("/",response_class=HTMLResponse)
async def home():return open("index.html",encoding="utf8").read()
@app.get("/api/health")
async def health():
 d=db();ok=False
 if d is not None:
  try:d.command("ping");ok=True
  except:pass
 return {"status":"Benyagram online","mongodb":ok,"groq":bool(GROQ),"openweather":bool(OW)}
@app.post("/api/register")
async def register(x:Register):
 u=x.username.strip()
 if any(c.isspace() for c in u):raise HTTPException(400,"Имя пользователя без пробелов")
 if user(u):raise HTTPException(409,"Имя уже занято")
 rec={"username":u,"username_key":u.lower(),"display_name":x.display_name.strip(),"password_hash":phash(x.password),"created_at":now(),"role":"member"}
 d=db()
 if d is not None:
  try:d.users.insert_one(rec)
  except:users[u.lower()]=rec
 else:users[u.lower()]=rec
 r=JSONResponse({"ok":True,"user":{"username":u,"display_name":rec["display_name"]}})
 r.set_cookie("benyagram_session",token(u),httponly=True,samesite="lax",secure=True,max_age=2592000);return r
@app.post("/api/login")
async def login(x:Login):
 u=user(x.username)
 if not u or not verify(x.password,u.get("password_hash","")):raise HTTPException(401,"Неверный логин или пароль")
 r=JSONResponse({"ok":True,"user":{"username":u["username"],"display_name":u.get("display_name",u["username"])}})
 r.set_cookie("benyagram_session",token(u["username"]),httponly=True,samesite="lax",secure=True,max_age=2592000);return r
@app.post("/api/logout")
async def logout():
 r=JSONResponse({"ok":True});r.delete_cookie("benyagram_session");return r
@app.get("/api/me")
async def me(req:Request):
 u=auth(req);x=user(u) or {};return {"username":u,"display_name":x.get("display_name",u)}
@app.get("/api/messages")
async def get_messages(req:Request):auth(req);return {"messages":[clean(m) for m in allmsgs()]}
@app.websocket("/ws")
async def ws_endpoint(ws:WebSocket):
 u=who(ws.cookies.get("benyagram_session",""))
 if not u:await ws.close(code=4401);return
 await ws.accept();sockets.add(ws)
 try:
  while True:await ws.receive_text()
 except: sockets.discard(ws)
async def ai(prompt,u):
 if not GROQ:return "Добавьте GROQ_API_KEY в Render, чтобы включить ИИ. Сообщение сохранено."
 try:
  async with httpx.AsyncClient(timeout=45) as c:
   r=await c.post("https://api.groq.com/openai/v1/chat/completions",headers={"Authorization":"Bearer "+GROQ},json={"model":"llama-3.3-70b-versatile","messages":[{"role":"system","content":"Ты Benyagram AI — доброжелательный помощник общего чата. Отвечай на языке пользователя, кратко и полезно."},{"role":"user","content":f"Пользователь @{u} пишет тебе: {prompt}"}],"max_tokens":700})
  return r.json()["choices"][0]["message"]["content"].strip() if r.status_code==200 else "AI временно недоступен, попробуйте ещё раз."
 except:return "Не удалось соединиться с AI. Попробуйте ещё раз."
@app.post("/api/messages")
async def post(x:Message,req:Request):
 u=auth(req);usr=user(u) or {};txt=x.text.strip()
 if not txt:raise HTTPException(400,"Пустое сообщение")
 m={"id":secrets.token_urlsafe(10),"username":u,"display_name":usr.get("display_name",u),"text":txt,"created_at":now(),"edited_at":None,"deleted":False,"pinned":False,"reply_to":x.reply_to,"kind":"user"}
 save(m);await broadcast({"type":"message","message":clean(m)})
 out=[clean(m)]
 if txt.lower()=="ai" or txt[:3].lower()=="ai ":
  prompt=txt[2:].strip()
  if prompt:
   reply={"id":secrets.token_urlsafe(10),"username":"BenyagramAI","display_name":"Benyagram AI","text":await ai(prompt,u),"created_at":now(),"edited_at":None,"deleted":False,"pinned":False,"reply_to":m["id"],"kind":"assistant"}
   save(reply);await broadcast({"type":"message","message":clean(reply)});out.append(clean(reply))
 return {"messages":out}
@app.patch("/api/messages/{mid}")
async def edit(mid:str,x:Edit,req:Request):
 u=auth(req);old=next((m for m in allmsgs() if m["id"]==mid),None)
 if not old:raise HTTPException(404,"Сообщение не найдено")
 if old["username"]!=u:raise HTTPException(403,"Можно редактировать только свои сообщения")
 if old.get("deleted"):raise HTTPException(400,"Сообщение удалено")
 m=update(mid,{"text":x.text.strip(),"edited_at":now()},u)
 await broadcast({"type":"update","message":clean(m)});return clean(m)
@app.delete("/api/messages/{mid}")
async def delete(mid:str,req:Request):
 u=auth(req);old=next((m for m in allmsgs() if m["id"]==mid),None)
 if not old:raise HTTPException(404,"Сообщение не найдено")
 if old["username"]!=u:raise HTTPException(403,"Можно удалять только свои сообщения")
 m=update(mid,{"text":"Сообщение удалено","deleted":True,"edited_at":now()},u);await broadcast({"type":"update","message":clean(m)});return {"ok":True}
@app.post("/api/messages/{mid}/pin")
async def pin(mid:str,req:Request):
 auth(req);old=next((m for m in allmsgs() if m["id"]==mid),None)
 if not old:raise HTTPException(404,"Сообщение не найдено")
 m=update(mid,{"pinned":not old.get("pinned",False)});await broadcast({"type":"update","message":clean(m)});return clean(m)
@app.get("/api/geo/search")
async def geosearch(q:str):
 if not OW:raise HTTPException(503,"Добавьте OPENWEATHER_API_KEY в Render")
 async with httpx.AsyncClient(timeout=15) as c:r=await c.get("https://api.openweathermap.org/geo/1.0/direct",params={"q":q,"limit":1,"appid":OW})
 data=r.json()
 if not data:raise HTTPException(404,"Место не найдено")
 p=data[0];return {"lat":p["lat"],"lon":p["lon"],"name":", ".join(v for v in [p.get("name"),p.get("country")] if v)}
@app.get("/api/weather")
async def weather(lat:float,lon:float):
 if not OW:raise HTTPException(503,"Добавьте OPENWEATHER_API_KEY в Render")
 async with httpx.AsyncClient(timeout=15) as c:r=await c.get("https://api.openweathermap.org/data/2.5/weather",params={"lat":lat,"lon":lon,"appid":OW,"units":"metric"})
 if r.status_code!=200:raise HTTPException(502,"Погода недоступна")
 d=r.json();return {"name":d.get("name","Место на карте"),"temp":round(d.get("main",{}).get("temp",0)),"description":d.get("weather",[{}])[0].get("description",""),"icon":d.get("weather",[{}])[0].get("icon","01d"),"humidity":d.get("main",{}).get("humidity"),"wind":d.get("wind",{}).get("speed")}
  
