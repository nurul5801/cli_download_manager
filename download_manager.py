import os
import datetime
import time
import httpx
import asyncio
import re
import shutil
from urllib.parse import unquote, urlparse

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Header, Input, RichLog, ProgressBar, Static, Button
from textual.widget import Widget
from textual.worker import Worker, get_current_worker
from textual.reactive import reactive
from textual.message import Message

# --- Helper Functions ---
def parse_size_to_bytes(size_str: str) -> int:
    """Parses a size string (e.g., '12.34MiB') to bytes."""
    if not size_str or size_str == 'N/A':
        return 0
    
    size_str = size_str.upper()
    if size_str.endswith('B'):
        size_str = size_str[:-1] # Remove 'B'

    if size_str.endswith('K'):
        return int(float(size_str[:-1]) * 1024)
    elif size_str.endswith('M'):
        return int(float(size_str[:-1]) * 1024**2)
    elif size_str.endswith('G'):
        return int(float(size_str[:-1]) * 1024**3)
    else:
        try:
            return int(float(size_str))
        except ValueError:
            return 0

# --- Constants ---
DOWNLOADS_DIR = "downloads"
LOG_FILE = "download_history.log"

class DownloadItem(Widget):
    """A widget representing a single download item."""

    # --- Custom Messages ---
    class DownloadSuccess(Message):
        def __init__(self, url: str) -> None:
            self.url = url
            super().__init__()

    class DownloadFailed(Message):
        def __init__(self, url: str, error: str) -> None:
            self.url = url
            self.error = error
            super().__init__()

    class DownloadCancelled(Message):
        def __init__(self, url: str) -> None:
            self.url = url
            super().__init__()

    DEFAULT_CSS = """
    DownloadItem {
        layout: vertical;
        border: round $primary;
        margin: 1 0;
        height: auto;
    }
    .stats-container {
        layout: horizontal;
        height: auto;
    }
    .progress-display, .speed-display {
        width: 1fr;
    }
    .buttons-container {
        layout: horizontal;
        height: auto;
    }
    .buttons-container Button {
        width: 1fr;
    }
    """

    # --- Reactive properties for state ---
    bytes_downloaded = reactive(0)
    total_size = reactive(0)
    download_speed = reactive(0.0)
    is_paused = reactive(False)

    def __init__(self, url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.download_worker: Worker | None = None
        
        # --- URL Type Detection ---
        self.is_youtube_dl = bool(re.match(r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/", url))

        if self.is_youtube_dl:
            self.filename = "YouTube Video"
            self.download_path = os.path.join(DOWNLOADS_DIR)
        else:
            path = urlparse(self.url).path
            self.filename = unquote(os.path.basename(path))
            self.download_path = os.path.join(DOWNLOADS_DIR, self.filename)

    def compose(self) -> ComposeResult:
        yield Static(f"URL: {self.url}")
        with Container(classes="stats-container"):
            yield Static("Progress: N/A", classes="progress-display")
            yield Static("Speed: N/A", classes="speed-display")
        yield ProgressBar(total=100, show_eta=False)
        with Container(classes="buttons-container"):
            yield Button("Pause", variant="primary", id="pause-resume")
            yield Button("Cancel", variant="error", id="cancel")

    def on_mount(self) -> None:
        """Called when the widget is mounted."""
        self.start_download()

    def start_download(self, resume_from: int = 0) -> None:
        """Starts the download worker."""
        self.is_paused = False

        async def do_download() -> None:
            if self.is_youtube_dl:
                await self.download_youtube_video()
            else:
                await self.download_file(self.url, resume_from=resume_from)

        self.download_worker = self.run_worker(do_download)

    # --- Watch methods to update UI when reactive properties change ---
    def watch_bytes_downloaded(self, new_value: int) -> None:
        self.query_one(".progress-display").update(f"Progress: {new_value/1024/1024:.2f} / {self.total_size/1024/1024:.2f} MB")
        self.query_one(ProgressBar).update(progress=new_value)

    def watch_total_size(self, new_value: int) -> None:
        self.query_one(ProgressBar).total = new_value or 100

    def watch_download_speed(self, new_value: float) -> None:
        self.query_one(".speed-display").update(f"Speed: {new_value/1024/1024:.2f} MB/s")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses for this specific download item."""
        if event.button.id == "pause-resume":
            if self.is_paused:
                event.button.label = "Pause"
                self.start_download(resume_from=self.bytes_downloaded)
            else:
                self.is_paused = True
                event.button.label = "Resume"
                if self.download_worker:
                    self.download_worker.cancel()
        elif event.button.id == "cancel":
            if self.download_worker:
                self.download_worker.cancel()
            if os.path.exists(self.download_path):
                os.remove(self.download_path)
            self.post_message(self.DownloadCancelled(url=self.url))
            self.remove()

    async def download_file(self, url: str, resume_from: int = 0) -> None:
        """The main download logic for this item."""
        last_update_time = time.monotonic()
        last_downloaded = resume_from
        self.bytes_downloaded = resume_from

        try:
            headers = {}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
            
            file_mode = "ab" if resume_from > 0 else "wb"

            async with httpx.AsyncClient(follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code not in (200, 206):
                        response.raise_for_status()
                    
                    self.total_size = int(response.headers.get("content-length", 0))
                    if resume_from > 0:
                        self.total_size += resume_from

                    with open(self.download_path, file_mode) as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                            self.bytes_downloaded += len(chunk)
                            
                            current_time = time.monotonic()
                            time_diff = current_time - last_update_time
                            if time_diff > 0.5:
                                bytes_diff = self.bytes_downloaded - last_downloaded
                                self.download_speed = bytes_diff / time_diff if time_diff > 0 else 0
                                last_update_time = current_time
                                last_downloaded = self.bytes_downloaded
            
            # Success
            self.post_message(self.DownloadSuccess(url=self.url))
            self.remove()


        except asyncio.CancelledError:
            # Paused or Cancelled
            pass
        except Exception as e:
            self.post_message(self.DownloadFailed(url=self.url, error=str(e)))
            self.query_one("#pause-resume").disabled = True
            self.query_one("#cancel").label = "Failed"


    async def download_youtube_video(self) -> None:
        """Downloads a YouTube video using yt-dlp and updates progress."""
        progress_bar = self.query_one(ProgressBar)
        progress_display = self.query_one(".progress-display")
        speed_display = self.query_one(".speed-display")

        # Check if yt-dlp is available
        if not shutil.which("yt-dlp"):
            self.post_message(self.DownloadFailed(url=self.url, error="yt-dlp not found."))
            return

        # yt-dlp command
        # --progress: enable progress output
        # --newline: ensure each progress update is on a new line
        # --output: output template
        # --restrict-filenames: use only ASCII characters in filenames
        command = [
            "yt-dlp",
            "--progress",
            "--newline",
            "--output", os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s"),
            "--restrict-filenames",
            self.url
        ]

        # Regex to parse yt-dlp progress output
        # Example: [download]  10.3% of 12.34MiB at 1.23MiB/s ETA 00:08
        progress_re = re.compile(
            r".*\s+(\d+\.\d+)% of (\S+) at (\S+)/s ETA (\S+)"
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Read stderr line by line to get progress
            async def read_stream_and_log(stream, log_prefix: str):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    # self.log.info(f"{log_prefix}: {line.decode().strip()}") # For debugging

            await asyncio.gather(
                self._parse_yt_dlp_progress(process.stderr, progress_bar, progress_display, speed_display),
                read_stream_and_log(process.stdout, "YT-DLP STDOUT"),
            )

            await process.wait() # Wait for the process to finish

            if process.returncode == 0:
                self.post_message(self.DownloadSuccess(url=self.url))
                self.remove()
            else:
                # Read any remaining stderr for final error message
                remaining_stderr = (await process.stderr.read()).decode().strip()
                error_message = remaining_stderr if remaining_stderr else "Unknown yt-dlp error."
                self.post_message(self.DownloadFailed(url=self.url, error=f"yt-dlp failed: {error_message}"))

        except asyncio.CancelledError:
            # Kill the subprocess if the download is cancelled/paused
            if process.returncode is None:
                process.terminate()
                await process.wait()
            pass
        except Exception as e:
            self.post_message(self.DownloadFailed(url=self.url, error=f"yt-dlp execution error: {e}"))

    async def _parse_yt_dlp_progress(self, stderr_stream, progress_bar, progress_display, speed_display) -> None:
        """Parses yt-dlp stderr for progress updates."""
        progress_re = re.compile(
            r".*\s+(\d+\.\d+)% of (\S+) at (\S+)/s ETA (\S+)"
        )
        while True:
            line = await stderr_stream.readline()
            if not line:
                break
            line_str = line.decode().strip()
            
            match = progress_re.match(line_str)
            if match:
                percent_str, total_size_str, speed_str, eta_str = match.groups()
                
                # Update reactive properties
                self.bytes_downloaded = int(float(percent_str) / 100 * parse_size_to_bytes(total_size_str))
                self.total_size = parse_size_to_bytes(total_size_str)
                self.download_speed = parse_size_to_bytes(speed_str)
                
                # Update progress bar (yt-dlp gives percentage, so we can use that)
                progress_bar.update(progress=float(percent_str))
                progress_bar.total = 100 # Set total to 100 for percentage


class DownloadManagerApp(App):
    """A Textual download manager application."""

    CSS = """
    #main-container {
        layout: vertical;
    }
    #download-container {
        layout: vertical;
    }
    #log-viewer {
        height: 10;
    }
    """

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        yield Container(
            Input(placeholder="Enter a URL to download"),
            VerticalScroll(id="download-container"),
            id="main-container"
        )
        yield RichLog(wrap=True, id="log-viewer")

    def on_mount(self) -> None:
        """Set up directories and log files."""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w") as f:
                f.write("--- Download History ---\n")
        
        log_viewer = self.query_one("#log-viewer", RichLog)
        with open(LOG_FILE, "r") as f:
            log_viewer.write(f.read())

        if not shutil.which("yt-dlp"):
            log_viewer.write("[WARNING] yt-dlp not found. YouTube downloads will not be available.")

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        """Called when the user submits a URL."""
        url = message.value.strip()
        if url:
            self.query_one(Input).clear()
            new_download = DownloadItem(url=url)
            await self.query_one("#download-container").mount(new_download)
            log_viewer = self.query_one("#log-viewer", RichLog)
            log_viewer.write(f"Started download: {url}")

    def on_download_item_download_success(self, message: DownloadItem.DownloadSuccess) -> None:
        """Log a successful download."""
        log_viewer = self.query_one("#log-viewer", RichLog)
        log_viewer.write(f"[SUCCESS] Download finished: {message.url}")

    def on_download_item_download_failed(self, message: DownloadItem.DownloadFailed) -> None:
        """Log a failed download."""
        log_viewer = self.query_one("#log-viewer", RichLog)
        log_viewer.write(f"[FAILED] Download failed: {message.url} - {message.error}")

    def on_download_item_download_cancelled(self, message: DownloadItem.DownloadCancelled) -> None:
        """Log a cancelled download."""
        log_viewer = self.query_one("#log-viewer", RichLog)
        log_viewer.write(f"[CANCELLED] Download cancelled: {message.url}")


if __name__ == "__main__":
    app = DownloadManagerApp()
    app.run()