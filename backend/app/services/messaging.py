"""
WebSocket manager for real-time messaging.
Each user can have one active WebSocket connection.
When a message is sent, it's pushed to all participants who are online.
"""
from typing import Dict
from fastapi import WebSocket


class MessageConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}  # user_id -> websocket

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active[user_id] = websocket

    def disconnect(self, user_id: str):
        self.active.pop(user_id, None)

    async def send_to_user(self, user_id: str, data: dict):
        """Push a message event to a user if they're connected."""
        ws = self.active.get(user_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(user_id)


message_manager = MessageConnectionManager()
