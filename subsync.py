#!/usr/bin/env python3
"""
SubSync: A production-ready Python CLI tool for downloading, syncing, and generating subtitles.

Requirements (Install via pip):
    pip install requests beautifulsoup4 chardet rarfile faster-whisper tqdm

System Requirements for Generation:
    FFmpeg must be installed and accessible in your system's PATH.

Usage Examples:
    # Batch download English subtitles for a folder (skips existing, cleans HI tags/HTML)
    python subsync.py download "D:/Movies" -l en --clean

    # Shift existing subtitles forward by 2.5 seconds and clean tags
    python subsync.py sync /path/to/movie.en.srt --shift 2.5 --clean
    
    # Convert framerate from 23.976 to 25 fps
    python subsync.py sync /path/to/movie.en.srt --from-fps 23.976 --to-fps 25

    # Generate subtitles from local video audio using AI (Optimized for CPU)
    python subsync.py generate /path/to/movie.mkv --model small
"""

import argparse
import io
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast, Pattern, Match

# Suppress Hugging Face symlink warnings globally before any HF imports happen
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: Missing core external libraries.")
    print("Please install them using: pip install requests beautifulsoup4 chardet rarfile faster-whisper tqdm")
    sys.exit(1)

# Dynamically typed optional dependencies
chardet: Any = None
try:
    import chardet  # type: ignore
except ImportError:
    pass

rarfile: Any = None
try:
    import rarfile  # type: ignore
except ImportError:
    pass

faster_whisper: Any = None
try:
    from faster_whisper import WhisperModel  # type: ignore
    faster_whisper = True
except ImportError:
    pass

tqdm_mod: Any = None
try:
    from tqdm import tqdm  # type: ignore
    tqdm_mod = tqdm
except ImportError:
    pass


# --- Configuration & Constants ---
USER_AGENT: str = "SubSync-CLI/3.3 (Python 3.10+; Production Subtitle Engine)"
VIDEO_EXTENSIONS: set[str] = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}
DEFAULT_LANGUAGE: str = "en"
DEFAULT_PATH: Path = Path("D:/User/Downloads")
OPEN_SUBS_API_KEY: str = os.getenv("OPENSUBTITLES_API_KEY", "mock_api_key_for_demo")


# --- Logging Setup ---
def setup_logging(verbose: bool) -> logging.Logger:
    """Configures the root logger based on verbosity."""
    level: int = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # Suppress verbose third-party network and cache logs
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    
    if not verbose:
        logging.getLogger("whisper").setLevel(logging.WARNING)
        logging.getLogger("faster_whisper").setLevel(logging.WARNING)
        
    return logging.getLogger("SubSync")

logger: logging.Logger = logging.getLogger("SubSync")




# --- Core Utilities ---
class VideoMetadata:
    """Holds extracted metadata and hashes for a video file."""
    filepath: Path
    filename: str
    hash: Optional[str]
    parsed_data: Dict[str, Optional[Union[str, int]]]

    def __init__(self, filepath: Path) -> None:
        self.filepath = filepath
        self.filename = filepath.name
        self.parsed_data = self.parse_filename()
        self.hash = self.calculate_hash()

    def calculate_hash(self) -> Optional[str]:
        try:
            chunk_size: int = 65536
            longlongformat: str = "<q"
            bytesize: int = struct.calcsize(longlongformat)
            filesize: int = os.path.getsize(self.filepath)
            
            if filesize < chunk_size * 2:
                return None

            hash_val: int = filesize
            with open(self.filepath, "rb") as f:
                for _ in range(chunk_size // bytesize):
                    buffer: bytes = f.read(bytesize)
                    if len(buffer) < bytesize:
                        break
                    l_value: int = cast(int, struct.unpack(longlongformat, buffer)[0])
                    hash_val = (hash_val + l_value) & 0xFFFFFFFFFFFFFFFF

                f.seek(max(0, filesize - chunk_size), 0)
                for _ in range(chunk_size // bytesize):
                    buffer = f.read(bytesize)
                    if len(buffer) < bytesize:
                        break
                    l_value = cast(int, struct.unpack(longlongformat, buffer)[0])
                    hash_val = (hash_val + l_value) & 0xFFFFFFFFFFFFFFFF

            return "%016x" % hash_val
        except Exception as e:
            logger.error(f"Failed to hash {self.filename}: {e}")
            return None

    def parse_filename(self) -> Dict[str, Optional[Union[str, int]]]:
        data: Dict[str, Optional[Union[str, int]]] = {
            "title": self.filepath.stem, 
            "year": None, 
            "season": None, 
            "episode": None
        }
        
        year_match: Optional[Match[str]] = re.search(r'(19\d{2}|20\d{2})', str(data["title"]))
        if year_match:
            data["year"] = year_match.group(1)
            title_part: str = str(data["title"])[:year_match.start()].replace(".", " ").strip()
            if title_part:
                data["title"] = title_part

        se_match: Optional[Match[str]] = re.search(r'[Ss](\d{2})[Ee](\d{2})|(\d{1,2})x(\d{2})', self.filepath.stem)
        if se_match:
            if se_match.group(1) and se_match.group(2):
                data["season"] = int(se_match.group(1))
                data["episode"] = int(se_match.group(2))
            elif se_match.group(3) and se_match.group(4):
                data["season"] = int(se_match.group(3))
                data["episode"] = int(se_match.group(4))
                
        return data

# --- Text Processing & Encoding ---
class SubtitleProcessor:
    """Handles encoding detection, decoding, and subtitle text cleaning."""
    
    @staticmethod
    def decode_content(raw_data: bytes) -> str:
        if chardet is not None:
            detected: Dict[str, Any] = chardet.detect(raw_data)
            encoding: Optional[str] = detected.get('encoding')
            if encoding:
                try:
                    return raw_data.decode(encoding)
                except UnicodeDecodeError:
                    pass

        for enc in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1']:
            try:
                return raw_data.decode(enc)
            except UnicodeDecodeError:
                continue
        
        return raw_data.decode('utf-8', errors='ignore')

    @staticmethod
    def clean_text(text: str, remove_hi: bool = True, remove_html: bool = True, fix_ocr: bool = True) -> str:
        # Strip all carriage returns early to ensure regex behaves predictably
        text = text.replace('\r', '')
        
        if remove_html:
            text = re.sub(r'<[^>]+>', '', text)
            
        if remove_hi:
            text = re.sub(r'\[.*?\]', '', text)
            text = re.sub(r'\(.*?\)', '', text)
            text = re.sub(r'(?m)^-\s*$', '', text)
            # Catch musical note symbols and enclosed lyrics
            text = re.sub(r'♪.*?♪', '', text, flags=re.DOTALL)
            text = re.sub(r'♪', '', text)

        if fix_ocr:
            text = re.sub(r'\bl\b', 'I', text)
            text = re.sub(r'\|', 'I', text)
            
        # Clean up excessive blank lines left by tag removal
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        # BUG FIX: Remove orphaned subtitle blocks (Index + Timestamp but no text)
        text = re.sub(r'\d+\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\n(?=\n|$)', '', text)
        
        # Final formatting pass to ensure strict double-newline spacing
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip() + '\n'


# --- Sync / Shift Engine ---
class SubtitleSyncer:
    TIME_REGEX: Pattern[str] = re.compile(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})')

    @staticmethod
    def _time_to_ms(time_str: str) -> int:
        h, m, s_ms = time_str.split(':')
        s, ms = s_ms.split(',')
        return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

    @staticmethod
    def _ms_to_time(ms: int) -> str:
        ms = max(0, ms) 
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @classmethod
    def sync_srt(cls, filepath: Path, shift_seconds: float = 0.0, 
                 from_fps: Optional[float] = None, to_fps: Optional[float] = None, clean: bool = False) -> bool:
        shift_ms: int = int(shift_seconds * 1000)
        fps_multiplier: float = (from_fps / to_fps) if (from_fps and to_fps) else 1.0
        
        logger.info(f"Syncing {filepath.name} | Shift: {shift_seconds}s | FPS Factor: {fps_multiplier:.4f}")

        try:
            with open(filepath, 'rb') as f:
                raw_data: bytes = f.read()
                
            text: str = SubtitleProcessor.decode_content(raw_data)
            
            # BUG FIX: Strip invisible carriage returns entirely to prevent '\r\r\n' doubling on Windows
            text = text.replace('\r', '')
            
            if clean:
                text = SubtitleProcessor.clean_text(text)
                
            lines: List[str] = text.splitlines(keepends=True)
                
            shifted_lines: List[str] = []
            for line in lines:
                match: Optional[Match[str]] = cls.TIME_REGEX.search(line)
                if match:
                    start_ms: int = int(cls._time_to_ms(match.group(1)) * fps_multiplier) + shift_ms
                    end_ms: int = int(cls._time_to_ms(match.group(2)) * fps_multiplier) + shift_ms
                    
                    new_start: str = cls._ms_to_time(start_ms)
                    new_end: str = cls._ms_to_time(end_ms)
                    
                    shifted_line: str = (
                        line[:match.start(1)] + 
                        new_start + 
                        line[match.end(1):match.start(2)] + 
                        new_end + 
                        line[match.end(2):]
                    )
                    shifted_lines.append(shifted_line)
                else:
                    shifted_lines.append(line)

            # Python will automatically translate the standard '\n' back to a clean '\r\n' on Windows
            with open(filepath, 'w', encoding='utf-8') as f:
                f.writelines(shifted_lines)
                
            logger.info("Sync complete. File overwritten successfully in UTF-8.")
            return True
            
        except Exception as e:
            logger.error(f"Failed to sync subtitle: {e}")
            return False

# --- AI Generation Engine (Optimized CPU via faster-whisper) ---
class SubtitleGenerator:
    model_name: str
    model: Any
    physical_cores: int
    
    def __init__(self, model_name: str = "base") -> None:
        if faster_whisper is None:
            logger.error("faster-whisper not installed. Please run: pip install faster-whisper")
            sys.exit(1)
            
        if not self._check_ffmpeg():
            logger.error("FFmpeg is required but not found in PATH. Please install FFmpeg.")
            sys.exit(1)

        self.model_name = model_name
        self.model = None
        # Intel CPU optimization: Use physical cores instead of logical threads for faster-whisper/CTranslate2
        self.physical_cores = max(1, (os.cpu_count() or 4) // 2)

    def _check_ffmpeg(self) -> bool:
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def _format_timestamp(self, seconds: float) -> str:
        ms: int = int((seconds % 1) * 1000)
        m_total, s = divmod(int(seconds), 60)
        h, m = divmod(m_total, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def process_file(self, video_path: Path, lang: str = "en") -> None:
        srt_path: Path = video_path.with_suffix(f".{lang}.srt")
        
        if srt_path.exists():
            logger.info(f"Subtitle already exists: {srt_path.name}. Skipping generation.")
            return

        logger.info(f"\nGenerating subtitles for: {video_path.name}")
        
        if self.model is None:
            logger.info(f"Loading faster-whisper model '{self.model_name}' (INT8 CPU Optimized)...")
            self.model = WhisperModel(
                self.model_name, 
                device="cpu", 
                compute_type="int8", 
                cpu_threads=self.physical_cores
            )

        tmp_audio_path: str = ""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_audio:
            tmp_audio_path = tmp_audio.name

        try:
            logger.info("Extracting audio stream via FFmpeg...")
            cmd: List[str] = [
                "ffmpeg", "-i", str(video_path),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                "-y", tmp_audio_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            logger.info(f"Transcribing audio using {self.physical_cores} CPU threads...")
            kwargs: Dict[str, Any] = {"task": "transcribe"}
            if lang and lang.lower() != "auto":
                kwargs["language"] = lang

            segments, info = self.model.transcribe(tmp_audio_path, **kwargs)
            
            logger.info(f"Detected language '{info.language}' with probability {info.language_probability:.2f}")
            logger.info(f"Writing to {srt_path.name} in real-time...")

            with open(srt_path, "w", encoding="utf-8") as f:
                if tqdm_mod is not None:
                    total_duration = info.duration
                    # Extract just the HH:MM:SS part from the timestamp
                    total_str = self._format_timestamp(total_duration)[:8]
                    
                    # Override the default format to use our postfix for HH:MM:SS / HH:MM:SS
                    bar_fmt = "{l_bar}{bar}| {postfix} audio processed"
                    
                    with tqdm_mod(total=total_duration, bar_format=bar_fmt) as pbar:
                        pbar.set_postfix_str(f"00:00:00 / {total_str}")
                        for i, segment in enumerate(segments, start=1):
                            start_time: str = self._format_timestamp(segment.start)
                            end_time: str = self._format_timestamp(segment.end)
                            text: str = segment.text.strip()
                            f.write(f"{i}\n{start_time} --> {end_time}\n{text}\n\n")
                            
                            # Update postfix with formatted HH:MM:SS of current segment end
                            pbar.set_postfix_str(f"{end_time[:8]} / {total_str}")
                            pbar.update(segment.end - segment.start)
                else:
                    for i, segment in enumerate(segments, start=1):
                        start_time = self._format_timestamp(segment.start)
                        end_time = self._format_timestamp(segment.end)
                        text = segment.text.strip()
                        f.write(f"{i}\n{start_time} --> {end_time}\n{text}\n\n")
                        print(f"Processed: {start_time} --> {end_time}", end="\r")
                    
            logger.info("\nGeneration successful.")
            
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg audio extraction failed: {e}")
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
        finally:
            if os.path.exists(tmp_audio_path):
                try:
                    os.remove(tmp_audio_path)
                except Exception:
                    pass

    def process_path(self, target_path: Path, lang: str = "en") -> None:
        if target_path.is_file():
            if target_path.suffix.lower() in VIDEO_EXTENSIONS:
                self.process_file(target_path, lang)
            else:
                logger.error(f"File {target_path.name} is not a supported video extension.")
        elif target_path.is_dir():
            videos: List[Path] = [p for p in target_path.rglob("*") if p.suffix.lower() in VIDEO_EXTENSIONS]
            if not videos:
                logger.warning(f"No video files found in {target_path}")
                return
            logger.info(f"Found {len(videos)} video files for AI generation batch processing.")
            for video in videos:
                self.process_file(video, lang)
        else:
            logger.error(f"Path does not exist: {target_path}")


# --- Subtitle Web Providers ---
class SubtitleProvider(ABC):
    session: requests.Session

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        
    @abstractmethod
    def find_and_download(self, video: VideoMetadata, lang: str) -> Optional[bytes]:
        pass

class OpenSubtitlesAPI(SubtitleProvider):
    base_url: str

    def __init__(self) -> None:
        super().__init__()
        self.base_url = "https://api.opensubtitles.com/api/v1"
        self.session.headers.update({"Api-Key": OPEN_SUBS_API_KEY})

    def find_and_download(self, video: VideoMetadata, lang: str) -> Optional[bytes]:
        if not video.hash: 
            return None
        logger.info(f"[OpenSubtitles] Searching via file hash: {video.hash}")
        try:
            res: requests.Response = self.session.get(f"{self.base_url}/subtitles", params={"moviehash": video.hash, "languages": lang}, timeout=10)
            if res.status_code in (401, 403, 429):
                logger.warning(f"[OpenSubtitles] API Key rejected or rate limited ({res.status_code}).")
                return None
            res.raise_for_status()
            
            data: Dict[str, Any] = res.json()
            if data.get("data"):
                file_id: str = data["data"][0]["attributes"]["files"][0]["file_id"]
                dl_req: requests.Response = self.session.post(f"{self.base_url}/download", json={"file_id": file_id}, timeout=10)
                dl_req.raise_for_status()
                dl_link: Optional[str] = dl_req.json().get("link")
                
                if dl_link:
                    logger.debug("[OpenSubtitles] Downloading file...")
                    return self.session.get(dl_link, timeout=15).content
        except Exception as e:
            logger.debug(f"[OpenSubtitles] Error: {e}")
        return None

class PodnapisiAPI(SubtitleProvider):
    base_url: str

    def __init__(self) -> None:
        super().__init__()
        self.base_url = "https://www.podnapisi.net/subtitles/search/advanced"

    def find_and_download(self, video: VideoMetadata, lang: str) -> Optional[bytes]:
        title_val = video.parsed_data.get('title')
        title_str: str = str(title_val) if title_val else ""
        
        logger.info(f"[Podnapisi] Searching via text fallback: {title_str}")
        params: Dict[str, Any] = {
            "keywords": title_str, 
            "year": video.parsed_data.get('year') or "", 
            "language": lang
        }
        
        season_val = video.parsed_data.get('season')
        episode_val = video.parsed_data.get('episode')
        
        if season_val and episode_val:
            params.update({
                "seasons": season_val, 
                "episodes": episode_val
            })
            
        try:
            res: requests.Response = self.session.get(self.base_url, params=params, timeout=10)
            res.raise_for_status()
            soup: BeautifulSoup = BeautifulSoup(res.text, 'html.parser')
            
            download_row = soup.find('a', href=re.compile(r'/subtitles/.+/download'))
            if download_row and hasattr(download_row, 'get'):
                href: Optional[str] = cast(Optional[str], download_row.get('href'))
                if href:
                    dl_url: str = f"https://www.podnapisi.net{href}"
                    logger.debug(f"[Podnapisi] Downloading from: {dl_url}")
                    return self.session.get(dl_url, timeout=15).content
        except Exception as e:
            logger.debug(f"[Podnapisi] Error or not found: {e}")
        return None

class SubsceneScraper(SubtitleProvider):
    base_url: str

    def __init__(self) -> None:
        super().__init__()
        self.base_url = "https://subscene.com"

    def find_and_download(self, video: VideoMetadata, lang: str) -> Optional[bytes]:
        title_val = video.parsed_data.get('title')
        title_str: str = str(title_val) if title_val else ""
        
        if not title_str:
            return None

        logger.info(f"[Subscene] Searching for: {title_str}")
        try:
            search_res: requests.Response = self.session.post(f"{self.base_url}/subtitles/searchbytitle", data={"query": title_str}, timeout=10)
            soup: BeautifulSoup = BeautifulSoup(search_res.text, 'html.parser')
            
            title_link = soup.find('a', string=re.compile(title_str, re.IGNORECASE))
            if not title_link or not hasattr(title_link, 'get'): 
                return None
            
            title_href: Optional[str] = cast(Optional[str], title_link.get('href'))
            if not title_href:
                return None

            movie_res: requests.Response = self.session.get(f"{self.base_url}{title_href}", timeout=10)
            movie_soup: BeautifulSoup = BeautifulSoup(movie_res.text, 'html.parser')
            
            lang_mapping: Dict[str, str] = {"en": "English", "fr": "French", "es": "Spanish"}
            target_lang: str = lang_mapping.get(lang, "English")
            
            sub_row = movie_soup.find('span', string=re.compile(target_lang, re.IGNORECASE))
            if not sub_row: 
                return None
            
            parent_a = sub_row.find_parent('a')
            if not parent_a or not hasattr(parent_a, 'get'):
                return None
            
            sub_href: Optional[str] = cast(Optional[str], parent_a.get('href'))
            if not sub_href:
                return None

            dl_page_res: requests.Response = self.session.get(f"{self.base_url}{sub_href}", timeout=10)
            dl_soup: BeautifulSoup = BeautifulSoup(dl_page_res.text, 'html.parser')
            dl_btn = dl_soup.find('a', id='downloadButton')
            
            if dl_btn and hasattr(dl_btn, 'get'):
                dl_btn_href: Optional[str] = cast(Optional[str], dl_btn.get('href'))
                if dl_btn_href:
                    logger.debug("[Subscene] Downloading archive...")
                    return self.session.get(f"{self.base_url}{dl_btn_href}", timeout=15).content
        except Exception as e:
            logger.debug(f"[Subscene] Scraping blocked or failed: {e}")
        return None
        
class Addic7edScraper(SubtitleProvider):
    base_url: str

    def __init__(self) -> None:
        super().__init__()
        self.base_url = "https://www.addic7ed.com"

    def find_and_download(self, video: VideoMetadata, lang: str) -> Optional[bytes]:
        raw_season = video.parsed_data.get('season')
        raw_episode = video.parsed_data.get('episode')
        
        if not raw_season or not raw_episode: 
            return None 
            
        title_val = video.parsed_data.get('title')
        title_str: str = str(title_val) if title_val else ""
        season: int = int(raw_season)
        episode: int = int(raw_episode)

        logger.info(f"[Addic7ed] Searching for TV Show: {title_str}")
        try:
            search_url: str = f"{self.base_url}/search.php"
            params: Dict[str, str] = {
                "search": f"{title_str} s{season:02d}e{episode:02d}", 
                "Submit": "Search"
            }
            self.session.headers.update({"Referer": self.base_url})
            
            res: requests.Response = self.session.get(search_url, params=params, timeout=10)
            soup: BeautifulSoup = BeautifulSoup(res.text, 'html.parser')
            
            dl_btn = soup.find('a', href=re.compile(r'/original/\d+/\d+'))
            if dl_btn and hasattr(dl_btn, 'get'):
                href: Optional[str] = cast(Optional[str], dl_btn.get('href'))
                if href:
                    dl_link: str = f"{self.base_url}{href}"
                    return self.session.get(dl_link, timeout=15).content
        except Exception as e:
            logger.debug(f"[Addic7ed] Scraping failed: {e}")
        return None


# --- Download Orchestrator ---
class SubtitleDownloader:
    lang: str
    clean_subs: bool
    providers: List[SubtitleProvider]

    def __init__(self, target_lang: str, clean_subs: bool) -> None:
        self.lang = target_lang
        self.clean_subs = clean_subs
        self.providers = [
            OpenSubtitlesAPI(),
            PodnapisiAPI(),
            Addic7edScraper(),
            SubsceneScraper(), 
        ]

    def _extract_srt_content(self, raw_data: bytes) -> Optional[bytes]:
        if raw_data.startswith(b'PK\x03\x04'):
            try:
                with zipfile.ZipFile(io.BytesIO(raw_data)) as z:
                    srt_file: Optional[str] = next((f for f in z.namelist() if f.endswith('.srt')), None)
                    return z.read(srt_file) if srt_file else None
            except Exception as e:
                logger.error(f"Failed to extract ZIP archive: {e}")
                return None
                
        elif raw_data.startswith(b'Rar!\x1a\x07'):
            if rarfile is None:
                logger.error("RAR archive detected but 'rarfile' library is not installed.")
                return None
            try:
                with rarfile.RarFile(io.BytesIO(raw_data)) as r:
                    srt_file = next((f for f in r.namelist() if f.endswith('.srt')), None)
                    return r.read(srt_file) if srt_file else None
            except Exception as e:
                logger.error(f"Failed to extract RAR archive: {e}")
                return None
                
        return raw_data

    def _process_and_save(self, raw_srt_bytes: bytes, video_path: Path) -> bool:
        target_srt_path: Path = video_path.with_suffix(f".{self.lang}.srt")
        try:
            text_content: str = SubtitleProcessor.decode_content(raw_srt_bytes)
            if self.clean_subs:
                text_content = SubtitleProcessor.clean_text(text_content)
                
            with open(target_srt_path, 'w', encoding='utf-8') as f:
                f.write(text_content)
                
            logger.info(f"Successfully saved cleanly to: {target_srt_path.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to write SRT file: {e}")
            return False

    def process_file(self, filepath: Path) -> None:
        target_srt_path: Path = filepath.with_suffix(f".{self.lang}.srt")
        if target_srt_path.exists():
            logger.info(f"Subtitle already exists: {target_srt_path.name}. Skipping download.")
            return

        logger.info(f"\nProcessing: {filepath.name}")
        video_meta: VideoMetadata = VideoMetadata(filepath)

        for provider in self.providers:
            provider_name: str = provider.__class__.__name__.replace("API", "").replace("Scraper", "")
            try:
                raw_payload: Optional[bytes] = provider.find_and_download(video_meta, self.lang)
                if raw_payload:
                    logger.info(f"[{provider_name}] Download successful.")
                    srt_bytes: Optional[bytes] = self._extract_srt_content(raw_payload)
                    if srt_bytes and self._process_and_save(srt_bytes, filepath):
                        return 
            except Exception as e:
                logger.debug(f"[{provider_name}] Unexpected error: {e}")
                
        logger.warning(f"Could not find subtitles for {filepath.name} across any web providers.")

    def process_path(self, target_path: Path) -> None:
        if target_path.is_file():
            if target_path.suffix.lower() in VIDEO_EXTENSIONS:
                self.process_file(target_path)
            else:
                logger.error(f"File {target_path.name} is not a supported video extension.")
        elif target_path.is_dir():
            videos: List[Path] = [p for p in target_path.rglob("*") if p.suffix.lower() in VIDEO_EXTENSIONS]
            if not videos:
                logger.warning(f"No video files found in {target_path}")
                return
            logger.info(f"Found {len(videos)} video files for batch processing.")
            for video in videos:
                self.process_file(video)
        else:
            logger.error(f"Path does not exist: {target_path}")


# --- CLI Interface ---
def main() -> None:
    parser = argparse.ArgumentParser(
        description="SubSync: Production-ready Subtitle Downloader, Synchronizer & AI Generator",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    # Command: Download
    dl_parser = subparsers.add_parser("download", help="Download subtitles for a video file or directory")
    dl_parser.add_argument("path", type=Path, nargs='?', default=DEFAULT_PATH, help=f"Path to the video file or directory (Default: {DEFAULT_PATH})")
    dl_parser.add_argument("-l", "--lang", default=DEFAULT_LANGUAGE, help="Language code (e.g., en, fr). Default: en")
    dl_parser.add_argument("--clean", action="store_true", help="Clean hearing-impaired tags, HTML, and OCR typos")
    dl_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    # Command: Sync
    sync_parser = subparsers.add_parser("sync", help="Shift/Sync existing subtitle timestamps globally")
    sync_parser.add_argument("path", type=Path, nargs='?', default=DEFAULT_PATH, help=f"Path to the .srt file or directory for batch syncing (Default: {DEFAULT_PATH})")
    sync_parser.add_argument("--shift", type=float, default=0.0, help="Time in seconds to shift (e.g., 1.5 to delay, -0.5 to advance)")
    sync_parser.add_argument("--from-fps", type=float, help="Original framerate of the subtitle (e.g., 23.976)")
    sync_parser.add_argument("--to-fps", type=float, help="Target framerate of the video (e.g., 25)")
    sync_parser.add_argument("--clean", action="store_true", help="Clean hearing-impaired tags, HTML, and OCR typos while syncing")
    sync_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    # Command: Generate (AI STT)
    gen_parser = subparsers.add_parser("generate", help="Generate subtitles from video audio using AI (requires FFmpeg and faster-whisper)")
    gen_parser.add_argument("path", type=Path, nargs='?', default=DEFAULT_PATH, help=f"Path to the video file or directory (Default: {DEFAULT_PATH})")
    gen_parser.add_argument("-l", "--lang", default=DEFAULT_LANGUAGE, help="Language to transcribe into. Default: en")
    gen_parser.add_argument("--model", default="base", choices=["tiny", "base", "small", "medium", "large"], help="Whisper AI model size to use (Default: base)")
    gen_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    args: argparse.Namespace = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "download":
        target_path: Path = cast(Path, args.path).resolve()
        downloader: SubtitleDownloader = SubtitleDownloader(target_lang=str(args.lang).lower(), clean_subs=args.clean)
        downloader.process_path(target_path)

    elif args.command == "sync":
        if (args.from_fps and not args.to_fps) or (args.to_fps and not args.from_fps):
            logger.error("Both --from-fps and --to-fps must be provided together.")
            sys.exit(1)
            
        target: Path = cast(Path, args.path).resolve()
        shift_val: float = cast(float, args.shift)
        from_fps_val: Optional[float] = cast(Optional[float], args.from_fps)
        to_fps_val: Optional[float] = cast(Optional[float], args.to_fps)
        clean_val: bool = cast(bool, args.clean)
        
        if target.is_file() and target.suffix.lower() == ".srt":
            SubtitleSyncer.sync_srt(target, shift_val, from_fps_val, to_fps_val, clean=clean_val)
        elif target.is_dir():
            srt_files: List[Path] = list(target.rglob("*.srt"))
            if not srt_files:
                logger.warning(f"No .srt files found in {target}")
            else:
                logger.info(f"Found {len(srt_files)} .srt files for batch syncing in {target}.")
                for srt in srt_files:
                    SubtitleSyncer.sync_srt(srt, shift_val, from_fps_val, to_fps_val, clean=clean_val)
        else:
            logger.error(f"Target path must be a valid .srt file or directory. Invalid: {target}")
            sys.exit(1)

    elif args.command == "generate":
        gen_target: Path = cast(Path, args.path).resolve()
        generator: SubtitleGenerator = SubtitleGenerator(model_name=str(args.model))
        generator.process_path(gen_target, lang=str(args.lang).lower())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        logger.fatal(f"An unexpected fatal error occurred: {e}", exc_info=True)
        sys.exit(1)