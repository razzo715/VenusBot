import json
import os
from pathlib import Path
import logging
import aiofiles

# Cache del contenuto del file (riletto solo se cambia): i ban vengono controllati a ogni comando vocale.
_FILE_CACHE = {"key": None, "content": None}


class BannedUsers:
    def __init__(self):
        root_dir = Path(__file__).resolve().parent
        self.BANNED_USERS_FILE = root_dir / "banned_users_list.json"
        if not self.BANNED_USERS_FILE.exists():
            self.BANNED_USERS_FILE.write_text(json.dumps([]), encoding="utf-8")

    async def loadBannedUserIDs(self) -> list[int]:
        try:
            st = os.stat(self.BANNED_USERS_FILE)
            file_key = (st.st_mtime_ns, st.st_size)
            if _FILE_CACHE["key"] != file_key:
                _FILE_CACHE["content"] = self.BANNED_USERS_FILE.read_text(encoding="utf-8")
                _FILE_CACHE["key"] = file_key
            content = _FILE_CACHE["content"]
            return json.loads(content) if content.strip() else []
        except Exception as e:
            logging.warning(f"Errore lettura banned_users_list.json: {e}")
            return []

    async def saveBannedUserIDs(self, user_ids: list[int]):
        async with aiofiles.open(self.BANNED_USERS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(user_ids, indent=2))
        _FILE_CACHE["key"] = None

    async def addBannedUserID(self, user_id: int):
        user_ids = await self.loadBannedUserIDs()
        if user_id not in user_ids:
            user_ids.append(user_id)
            await self.saveBannedUserIDs(user_ids)

    async def removeBannedUserID(self, user_id: int):
        user_ids = await self.loadBannedUserIDs()
        if user_id in user_ids:
            user_ids.remove(user_id)
            await self.saveBannedUserIDs(user_ids)
