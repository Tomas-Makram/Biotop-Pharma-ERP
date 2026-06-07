import datetime as dt
from pathlib import Path
import requests

import cloudinary
import cloudinary.uploader
import cloudinary.api
import os

cloud_name = ""
api_key = ""
api_secret = ""

class CloudinaryDbBackupManager:

    def __init__(self,folder: str = "db_backups",base_name: str = "erp_db",):
        cloudinary.config(cloud_name=cloud_name,api_key=api_key,api_secret=api_secret,secure=True,)
        self.folder = folder.strip("/")
        self.current_id = f"{self.folder}/{base_name}_current.db"
        self.prev_id = f"{self.folder}/{base_name}_prev.db"

    def _get_resource(self, public_id: str):
        return cloudinary.api.resource(public_id, resource_type="raw", type="upload")

    def exists(self, public_id: str) -> bool:
        try:
            self._get_resource(public_id)
            return True
        except Exception as e:
            msg = str(e).lower()
            if "not found" in msg or "resource not found" in msg or "404" in msg:
                return False
            raise

    def upload_rotate_two_versions(self, local_file_path: str) -> dict:
        p = Path(local_file_path)
        if not p.is_file():
            raise FileNotFoundError(f"DB file not found: {p}")

        uploaded_at = dt.datetime.now(dt.timezone.utc).isoformat()

        try:
            cloudinary.api.resource(self.current_id, resource_type="raw", type="upload")
            current_exists = True
        except Exception:
            current_exists = False

        if current_exists:
            try:
                cloudinary.uploader.destroy(self.prev_id, resource_type="raw", type="upload")
            except Exception:
                pass

            try:
                cloudinary.uploader.rename(
                    self.current_id,
                    self.prev_id,
                    resource_type="raw",
                    type="upload",
                    overwrite=True,
                )
            except Exception as e:
                print("Rename current->prev failed:", repr(e))

        result = cloudinary.uploader.upload(
            str(p),
            resource_type="raw",
            type="upload",
            public_id=self.current_id,
            overwrite=True,
            unique_filename=False,
            context={"uploaded_at": uploaded_at, "original_filename": p.name},
        )

        return {
            "uploaded_at": uploaded_at,
            "current_public_id": self.current_id,
            "prev_public_id": self.prev_id,
            "returned_public_id": result.get("public_id"),
            "bytes": result.get("bytes"),
        }

    def status(self) -> dict:
        return {
            "current_exists": self.exists(self.current_id),
            "prev_exists": self.exists(self.prev_id),
            "current_public_id": self.current_id,
            "prev_public_id": self.prev_id,
        }

    def download(self, which: str, dest_file_path: str) -> str:
        which = (which or "").lower().strip()
        if which not in ("current", "prev"):
            raise ValueError("which must be 'current' or 'prev'")

        public_id = self.current_id if which == "current" else self.prev_id

        res = self._get_resource(public_id)
        url = res.get("secure_url") or res.get("url")
        if not url:
            raise RuntimeError("Cloudinary resource has no download URL")

        out_path = Path(dest_file_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        r = requests.get(url, timeout=180)
        r.raise_for_status()
        out_path.write_bytes(r.content)
        return str(out_path)

backup_mgr = CloudinaryDbBackupManager(folder="db_backups", base_name="erp_db")