


from typing import List
from fastapi import (
    WebSocket,
)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def add_connection(self, websocket: WebSocket):
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        try:
            self.active_connections.remove(websocket)
        except:
            pass
        
    async def disconnect_all(self):
        print("disconnect all")
        for ws in self.active_connections:
            try:
                await ws.close()            
            except:
                print("error closing connection")
                pass

        self.active_connections = []

    async def send_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_json(message)
        except:
            pass

    async def broadcast(self, message: dict):
        to_disconnect = []
        for connection in self.active_connections:            
            try:
                await connection.send_json(message)                
            except:
                to_disconnect.append(connection)

        for connection in to_disconnect:
            try:
                await connection.close()
            except:
                pass

            try:
                self.disconnect(connection)
            except:
                pass
                
            


