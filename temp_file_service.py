"""
Temporary File Service - Manages temporary files with automatic cleanup
All files are stored in system temp directory and automatically deleted after use
"""
import os
import tempfile
import threading
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

class TemporaryFileService:
    """Service for managing temporary files with automatic cleanup"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TemporaryFileService, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Use system temp directory
        self.temp_base = tempfile.gettempdir()
        self.temp_dir = os.path.join(self.temp_base, 'shopify_automate_temp')
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # Track files by task_id
        self.task_files: Dict[str, List[str]] = {}
        self.file_timestamps: Dict[str, datetime] = {}
        self.file_lock = threading.Lock()
        
        # Cleanup old files on startup
        self.cleanup_old_files(max_age_hours=24)
        
        self._initialized = True
        logger.info(f"TemporaryFileService initialized with temp directory: {self.temp_dir}")
    
    def create_temp_file(self, prefix: str = 'shopify', suffix: str = '', task_id: Optional[str] = None) -> str:
        """
        Create a temporary file
        
        Args:
            prefix: File prefix
            suffix: File suffix (e.g., '.jpg', '.psd')
            task_id: Optional task ID to track this file
        
        Returns:
            Path to temporary file
        """
        fd, path = tempfile.mkstemp(prefix=f"{prefix}_", suffix=suffix, dir=self.temp_dir)
        os.close(fd)  # Close file descriptor, we'll open it when needed
        
        with self.file_lock:
            self.file_timestamps[path] = datetime.now()
            if task_id:
                if task_id not in self.task_files:
                    self.task_files[task_id] = []
                self.task_files[task_id].append(path)
        
        logger.debug(f"Created temp file: {path} (task_id: {task_id})")
        return path
    
    def create_temp_directory(self, prefix: str = 'shopify', task_id: Optional[str] = None) -> str:
        """
        Create a temporary directory
        
        Args:
            prefix: Directory prefix
            task_id: Optional task ID to track this directory
        
        Returns:
            Path to temporary directory
        """
        path = tempfile.mkdtemp(prefix=f"{prefix}_", dir=self.temp_dir)
        
        with self.file_lock:
            self.file_timestamps[path] = datetime.now()
            if task_id:
                if task_id not in self.task_files:
                    self.task_files[task_id] = []
                self.task_files[task_id].append(path)
        
        logger.debug(f"Created temp directory: {path} (task_id: {task_id})")
        return path
    
    def cleanup_after_task(self, task_id: str) -> int:
        """
        Cleanup all files associated with a task
        
        Args:
            task_id: Task ID to cleanup
        
        Returns:
            Number of files deleted
        """
        deleted_count = 0
        
        with self.file_lock:
            if task_id in self.task_files:
                files_to_delete = self.task_files[task_id].copy()
                del self.task_files[task_id]
            else:
                files_to_delete = []
        
        for path in files_to_delete:
            try:
                if os.path.isfile(path):
                    os.unlink(path)
                    deleted_count += 1
                    logger.debug(f"Deleted temp file: {path}")
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    deleted_count += 1
                    logger.debug(f"Deleted temp directory: {path}")
                
                with self.file_lock:
                    self.file_timestamps.pop(path, None)
            except Exception as e:
                logger.warning(f"Failed to delete temp file {path}: {e}")
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} files for task {task_id}")
        
        return deleted_count
    
    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """
        Cleanup files older than specified age
        
        Args:
            max_age_hours: Maximum age in hours
        
        Returns:
            Number of files deleted
        """
        deleted_count = 0
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        
        with self.file_lock:
            files_to_check = list(self.file_timestamps.items())
        
        for path, timestamp in files_to_check:
            if timestamp < cutoff_time:
                try:
                    if os.path.isfile(path):
                        os.unlink(path)
                        deleted_count += 1
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                        deleted_count += 1
                    
                    with self.file_lock:
                        self.file_timestamps.pop(path, None)
                        # Remove from task_files if present
                        for task_id, files in list(self.task_files.items()):
                            if path in files:
                                files.remove(path)
                                if not files:
                                    del self.task_files[task_id]
                except Exception as e:
                    logger.warning(f"Failed to delete old temp file {path}: {e}")
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old temp files (older than {max_age_hours}h)")
        
        return deleted_count
    
    def cleanup_all(self) -> int:
        """Cleanup all temporary files"""
        deleted_count = 0
        
        with self.file_lock:
            all_files = list(self.file_timestamps.keys())
            self.task_files.clear()
            self.file_timestamps.clear()
        
        for path in all_files:
            try:
                if os.path.isfile(path):
                    os.unlink(path)
                    deleted_count += 1
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    deleted_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete temp file {path}: {e}")
        
        logger.info(f"Cleaned up all {deleted_count} temp files")
        return deleted_count
    
    def get_temp_dir(self) -> str:
        """Get the base temporary directory"""
        return self.temp_dir
    
    def register_file(self, file_path: str, task_id: Optional[str] = None):
        """
        Register an existing file for tracking (e.g., created outside this service)
        
        Args:
            file_path: Path to file
            task_id: Optional task ID
        """
        with self.file_lock:
            self.file_timestamps[file_path] = datetime.now()
            if task_id:
                if task_id not in self.task_files:
                    self.task_files[task_id] = []
                self.task_files[task_id].append(file_path)
    
    def get_task_files(self, task_id: str) -> List[str]:
        """Get all files associated with a task"""
        with self.file_lock:
            return self.task_files.get(task_id, []).copy()

# Global instance
temp_file_service = TemporaryFileService()
