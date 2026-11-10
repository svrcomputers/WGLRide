"""
Rapido Clone - Complete Backend (With Cancel, Contact Info & Admin Controls)
Run: uvicorn main:app --reload
App: http://localhost:8000/app
"""

import os
import shutil
import requests
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ============================================================
# CONFIGURATION
# ============================================================
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN", "pk.YOUR_MAPBOX_TOKEN_HERE")
BASE_FARE = 20.0
PER_KM_RATE = 8.0
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ============================================================
# DATABASE SETUP
# ============================================================
DATABASE_URL = "sqlite:///./rapido.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============================================================
# MODELS
# ============================================================
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    mobile = Column(String, unique=True, index=True)
    address = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Driver(Base):
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    mobile = Column(String, unique=True, index=True)
    vehicle_type = Column(String, default="2W")
    rc_file = Column(String)
    dl_file = Column(String)
    status = Column(String, default="PENDING") # PENDING, APPROVED, REJECTED
    is_online = Column(Boolean, default=False)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

class Ride(Base):
    __tablename__ = "rides"
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=True)
    pickup_lat = Column(Float)
    pickup_lng = Column(Float)
    drop_lat = Column(Float)
    drop_lng = Column(Float)
    distance_km = Column(Float)
    fare = Column(Float)
    status = Column(String, default="REQUESTED") # REQUESTED, ACCEPTED, STARTED, COMPLETED, CANCELLED
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ============================================================
# SCHEMAS
# ============================================================
class CustomerRegister(BaseModel):
    name: str
    mobile: str
    address: str

class CustomerLogin(BaseModel):
    name: str
    mobile: str

class RideBook(BaseModel):
    customer_id: int
    pickup_lat: float
    pickup_lng: float
    drop_lat: float
    drop_lng: float

class DriverUpdate(BaseModel):
    name: str
    mobile: str
    vehicle_type: str

class FareUpdate(BaseModel):
    base_fare: float
    per_km_rate: float

# ============================================================
# APP & CORS
# ============================================================
app = FastAPI(title="Rapido Clone API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/app", StaticFiles(directory="static", html=True), name="static")

# ============================================================
# WEBSOCKET MANAGER
# ============================================================
class ConnectionManager:
    def __init__(self):
        self.driver_sockets = {}
        self.customer_sockets = {}

    async def connect_driver(self, driver_id, ws):
        await ws.accept()
        self.driver_sockets[driver_id] = ws

    async def connect_customer(self, customer_id, ws):
        await ws.accept()
        self.customer_sockets[customer_id] = ws

    def disconnect_driver(self, driver_id):
        self.driver_sockets.pop(driver_id, None)

    def disconnect_customer(self, customer_id):
        self.customer_sockets.pop(customer_id, None)

    async def send_to_driver(self, driver_id, message):
        ws = self.driver_sockets.get(driver_id)
        if ws:
            try: await ws.send_json(message)
            except: self.disconnect_driver(driver_id)

    async def send_to_customer(self, customer_id, message):
        ws = self.customer_sockets.get(customer_id)
        if ws:
            try: await ws.send_json(message)
            except: self.disconnect_customer(customer_id)

manager = ConnectionManager()

# ============================================================
# FARE CALCULATION
# ============================================================
def haversine(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return round(2 * R * atan2(sqrt(a), sqrt(1-a)), 2)

def calculate_fare(plat, plng, dlat, dlng):
    url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{plng},{plat};{dlng},{dlat}"
    try:
        r = requests.get(url, params={"access_token": MAPBOX_TOKEN, "overview": "false"}, timeout=5)
        routes = r.json().get("routes", [])
        if routes:
            dist = round(routes[0]["distance"] / 1000, 2)
            dur = round(routes[0]["duration"] / 60, 1)
        else:
            dist = haversine(plat, plng, dlat, dlng); dur = round(dist * 3, 1)
    except:
        dist = haversine(plat, plng, dlat, dlng); dur = round(dist * 3, 1)
    fare = round(BASE_FARE + dist * PER_KM_RATE, 2)
    return {"distance_km": dist, "duration_min": dur, "fare": fare}

# ============================================================
# CUSTOMER APIs
# ============================================================
@app.post("/api/customer/register")
def customer_register(data: CustomerRegister, db: Session = Depends(get_db)):
    if db.query(Customer).filter(Customer.mobile == data.mobile).first():
        raise HTTPException(400, "Mobile already registered")
    c = Customer(name=data.name, mobile=data.mobile, address=data.address)
    db.add(c); db.commit(); db.refresh(c)
    return {"message": "Registered", "customer_id": c.id}

@app.post("/api/customer/login")
def customer_login(data: CustomerLogin, db: Session = Depends(get_db)):
    c = db.query(Customer).filter(Customer.name == data.name, Customer.mobile == data.mobile).first()
    if not c: raise HTTPException(401, "Invalid credentials")
    return {"message": "Login ok", "customer_id": c.id, "name": c.name, "mobile": c.mobile, "address": c.address}

# ============================================================
# RIDE LIFECYCLE
# ============================================================
@app.post("/api/ride/estimate")
def ride_estimate(data: RideBook):
    return calculate_fare(data.pickup_lat, data.pickup_lng, data.drop_lat, data.drop_lng)

@app.post("/api/ride/book")
async def book_ride(data: RideBook, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == data.customer_id).first()
    if not customer: raise HTTPException(404, "Customer not found")
    
    info = calculate_fare(data.pickup_lat, data.pickup_lng, data.drop_lat, data.drop_lng)
    ride = Ride(customer_id=data.customer_id, pickup_lat=data.pickup_lat, pickup_lng=data.pickup_lng,
                drop_lat=data.drop_lat, drop_lng=data.drop_lng, distance_km=info["distance_km"],
                fare=info["fare"], status="REQUESTED")
    db.add(ride); db.commit(); db.refresh(ride)

    drivers = db.query(Driver).filter(Driver.status == "APPROVED", Driver.is_online == True).all()
    notif = {"type": "NEW_RIDE", "ride_id": ride.id, "pickup": {"lat": ride.pickup_lat, "lng": ride.pickup_lng},
             "drop": {"lat": ride.drop_lat, "lng": ride.drop_lng}, "distance_km": ride.distance_km, "fare": ride.fare,
             "customer_name": customer.name, "customer_mobile": customer.mobile}
             
    for d in drivers: await manager.send_to_driver(d.id, notif)
    return {"message": "Booked", "ride_id": ride.id, "fare": ride.fare}

@app.post("/api/ride/{ride_id}/accept")
async def accept_ride(ride_id: int, driver_id: int, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.id == driver_id).first()
    if not d or d.status != "APPROVED": raise HTTPException(403, "Driver not approved")
    
    updated = db.query(Ride).filter(Ride.id == ride_id, Ride.status == "REQUESTED").update(
        {"driver_id": driver_id, "status": "ACCEPTED"})
    db.commit()
    if updated == 0: raise HTTPException(409, "Already accepted or cancelled")
    
    ride = db.query(Ride).filter(Ride.id == ride_id).first()
    customer = db.query(Customer).filter(Customer.id == ride.customer_id).first()

    # డ్రైవర్ కి కస్టమర్ వివరాలు పంపడం
    await manager.send_to_driver(driver_id, {"type": "RIDE_DETAILS", "ride_id": ride.id,
        "pickup": {"lat": ride.pickup_lat, "lng": ride.pickup_lng}, "drop": {"lat": ride.drop_lat, "lng": ride.drop_lng},
        "customer_name": customer.name, "customer_mobile": customer.mobile, "customer_address": customer.address})
        
    # కస్టమర్ కి డ్రైవర్ వివరాలు పంపడం
    await manager.send_to_customer(ride.customer_id, {"type": "RIDE_ACCEPTED", "ride_id": ride.id,
        "driver": {"id": d.id, "name": d.name, "mobile": d.mobile, "vehicle_type": d.vehicle_type}})
    return {"message": "Accepted"}

@app.post("/api/ride/{ride_id}/start")
async def start_ride(ride_id: int, driver_id: int, db: Session = Depends(get_db)):
    ride = db.query(Ride).filter(Ride.id == ride_id, Ride.driver_id == driver_id).first()
    if not ride: raise HTTPException(404, "Not found")
    ride.status = "STARTED"; db.commit()
    await manager.send_to_customer(ride.customer_id, {"type": "RIDE_STARTED", "ride_id": ride.id})
    return {"message": "Started"}

@app.post("/api/ride/{ride_id}/complete")
async def complete_ride(ride_id: int, driver_id: int, db: Session = Depends(get_db)):
    ride = db.query(Ride).filter(Ride.id == ride_id, Ride.driver_id == driver_id).first()
    if not ride: raise HTTPException(404, "Not found")
    ride.status = "COMPLETED"; db.commit()
    await manager.send_to_customer(ride.customer_id, {"type": "RIDE_COMPLETED", "ride_id": ride.id, "fare": ride.fare})
    # రైడ్ కంప్లీట్ అయ్యాక డ్రైవర్ కి కస్టమర్ వివరాలు పోకుండా క్లియర్ చేయడం
    await manager.send_to_driver(driver_id, {"type": "CLEAR_CUSTOMER_INFO", "ride_id": ride.id})
    return {"message": "Completed", "fare": ride.fare}

@app.post("/api/ride/{ride_id}/cancel")
async def cancel_ride(ride_id: int, role: str, user_id: int, db: Session = Depends(get_db)):
    ride = db.query(Ride).filter(Ride.id == ride_id).first()
    if not ride: raise HTTPException(404, "Ride not found")
    
    if role == "customer" and ride.customer_id != user_id:
        raise HTTPException(403, "Unauthorized")
    if role == "driver" and ride.driver_id != user_id:
        raise HTTPException(403, "Unauthorized")

    ride.status = "CANCELLED"; db.commit()
    
    msg = {"type": "RIDE_CANCELLED", "ride_id": ride.id, "cancelled_by": role}
    await manager.send_to_customer(ride.customer_id, msg)
    if ride.driver_id: await manager.send_to_driver(ride.driver_id, msg)
    
    return {"message": "Cancelled"}

@app.post("/api/driver/{driver_id}/location")
async def update_location(driver_id: int, lat: float, lng: float, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.id == driver_id).first()
    if not d: raise HTTPException(404, "Not found")
    d.latitude, d.longitude = lat, lng; db.commit()
    
    ride = db.query(Ride).filter(Ride.driver_id == driver_id, Ride.status.in_(["ACCEPTED", "STARTED"])).first()
    if ride:
        await manager.send_to_customer(ride.customer_id, {"type": "DRIVER_LOCATION", "driver_id": driver_id, "lat": lat, "lng": lng})
    return {"message": "Updated"}

# ============================================================
# DRIVER APIs
# ============================================================
@app.post("/api/driver/register")
async def driver_register(name: str = Form(...), mobile: str = Form(...), vehicle_type: str = Form("2W"),
                          rc_file: UploadFile = File(...), dl_file: UploadFile = File(...), db: Session = Depends(get_db)):
    if db.query(Driver).filter(Driver.mobile == mobile).first():
        raise HTTPException(400, "Mobile already registered")
    rc_path = f"{UPLOAD_DIR}/{mobile}_RC_{rc_file.filename}"
    dl_path = f"{UPLOAD_DIR}/{mobile}_DL_{dl_file.filename}"
    with open(rc_path, "wb") as f: shutil.copyfileobj(rc_file.file, f)
    with open(dl_path, "wb") as f: shutil.copyfileobj(dl_file.file, f)
    d = Driver(name=name, mobile=mobile, vehicle_type=vehicle_type, rc_file=rc_path, dl_file=dl_path, status="PENDING")
    db.add(d); db.commit(); db.refresh(d)
    return {"message": "Submitted", "driver_id": d.id}

@app.post("/api/driver/login")
def driver_login(mobile: str, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.mobile == mobile).first()
    if not d: raise HTTPException(401, "Not found")
    return {"driver_id": d.id, "name": d.name, "mobile": d.mobile, "status": d.status, "is_online": d.is_online}

@app.post("/api/driver/{driver_id}/online")
def set_online(driver_id: int, is_online: bool, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.id == driver_id).first()
    if not d or d.status != "APPROVED": raise HTTPException(403, "Not approved")
    d.is_online = is_online; db.commit()
    return {"message": "OK"}

# ============================================================
# ADMIN APIs
# ============================================================
@app.get("/api/admin/stats")
def admin_stats(db: Session = Depends(get_db)):
    return {
        "total_customers": db.query(Customer).count(), "total_drivers": db.query(Driver).count(),
        "pending_drivers": db.query(Driver).filter(Driver.status == "PENDING").count(),
        "online_drivers": db.query(Driver).filter(Driver.is_online == True).count(),
        "total_rides": db.query(Ride).count(),
        "completed_rides": db.query(Ride).filter(Ride.status == "COMPLETED").count(),
        "current_fare": {"base_fare": BASE_FARE, "per_km_rate": PER_KM_RATE}
    }

@app.get("/api/admin/customers")
def admin_get_customers(db: Session = Depends(get_db)):
    return db.query(Customer).all()

@app.get("/api/admin/drivers")
def admin_get_drivers(status: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Driver)
    if status: q = q.filter(Driver.status == status)
    return q.all()

@app.get("/api/admin/rides")
def admin_get_rides(db: Session = Depends(get_db)):
    return db.query(Ride).order_by(Ride.id.desc()).all()

@app.post("/api/admin/driver/{driver_id}/approve")
def admin_approve_driver(driver_id: int, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.id == driver_id).first()
    if not d: raise HTTPException(404, "Not found")
    d.status = "APPROVED"; db.commit()
    return {"message": "Approved"}

@app.put("/api/admin/driver/{driver_id}")
def admin_update_driver(driver_id: int, data: DriverUpdate, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.id == driver_id).first()
    if not d: raise HTTPException(404, "Not found")
    d.name = data.name; d.mobile = data.mobile; d.vehicle_type = data.vehicle_type
    db.commit()
    return {"message": "Updated"}

@app.delete("/api/admin/driver/{driver_id}")
def admin_delete_driver(driver_id: int, db: Session = Depends(get_db)):
    d = db.query(Driver).filter(Driver.id == driver_id).first()
    if not d: raise HTTPException(404, "Not found")
    db.delete(d); db.commit()
    return {"message": "Deleted"}

@app.post("/api/admin/fare")
def admin_update_fare(data: FareUpdate):
    global BASE_FARE, PER_KM_RATE
    BASE_FARE = data.base_fare
    PER_KM_RATE = data.per_km_rate
    return {"message": "Fare updated"}

# ============================================================
# WEBSOCKETS
# ============================================================
@app.websocket("/ws/driver/{driver_id}")
async def ws_driver(ws: WebSocket, driver_id: int):
    await manager.connect_driver(driver_id, ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: manager.disconnect_driver(driver_id)

@app.websocket("/ws/customer/{customer_id}")
async def ws_customer(ws: WebSocket, customer_id: int):
    await manager.connect_customer(customer_id, ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: manager.disconnect_customer(customer_id)

@app.get("/favicon.ico")
def favicon(): return Response(status_code=204)

@app.get("/")
def root(): return RedirectResponse("/app")