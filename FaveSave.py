from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import html
import json
from json import dump as json_dump, load as json_load
from os import listdir, makedirs, path
import os
import re
import sys
import threading
import time

from PyQt6.QtCore import QCoreApplication, QDate, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import yt_dlp
from yt_dlp.utils import DownloadCancelled


URL_PATTERN = re.compile(r"(https?://[^\s<>\"]+)")


def make_links_clickable(message):
    message = str(message)
    parts = []
    last_index = 0
    for match in URL_PATTERN.finditer(message):
        parts.append(html.escape(message[last_index:match.start()]))
        url = match.group(0)
        if 'tiktok' in url.lower():
            safe_url = html.escape(url, quote=True)
            parts.append(f"<a href=\"{safe_url}\">{safe_url}</a>")
        else:
            parts.append(html.escape(url))
        last_index = match.end()
    parts.append(html.escape(message[last_index:]))
    return ''.join(parts)


# Session tracking functions for blocked videos
def load_session_data(download_folder):
    """Load blocked and failed videos from favesave_errors.json file in download folder"""
    session_file = path.join(download_folder, "favesave_errors.json")
    blocked_videos = set()
    failed_videos = set()
    
    if path.exists(session_file):
        try:
            with open(session_file, 'r') as f:
                session_data = json_load(f)
                blocked_videos = set(session_data.get('blocked', []))
                failed_videos = set(session_data.get('failed', []))
        except (json.JSONDecodeError, FileNotFoundError, KeyError):
            # If file is corrupted or doesn't exist, start with empty sets
            blocked_videos = set()
            failed_videos = set()
    
    return blocked_videos, failed_videos


def save_session_data(download_folder, blocked_videos, failed_videos):
    """Save blocked and failed videos to favesave_errors.json file in download folder"""
    session_file = path.join(download_folder, "favesave_errors.json")
    session_data = {
        'blocked': list(blocked_videos),
        'failed': list(failed_videos)
    }
    
    try:
        with open(session_file, 'w') as f:
            json_dump(session_data, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save session data: {e}")




# Determine the path to the logo based on whether the app is bundled
if hasattr(sys, '_MEIPASS'):
    logo_path = path.join(sys._MEIPASS, 'img', 'logo.png')
else:
    logo_path = path.join(path.dirname(__file__), 'img', 'logo.png')

# Function to load JSON file with explicit UTF-8 encoding
def load_json(json_file):
    try:
        with open(json_file, 'r', encoding='utf-8') as file:  # Explicitly set UTF-8 encoding
            return json_load(file)
    except UnicodeDecodeError as e:
        raise ValueError(f"Failed to decode JSON file. Ensure it's UTF-8 encoded. Error: {e}")
    except Exception as e:
        raise ValueError(f"Failed to load JSON file. Error: {e}")


# Function to get data from JSON with fallback logic
def get_activity_data(data, log_callback=None):
    """
    Get activity data from JSON with fallback logic.
    First tries 'Your Activity' node, then falls back to 'Likes and Favorites' node.
    """
    # Try 'Your Activity' first
    your_activity = data.get('Your Activity', {})
    if your_activity:
        if log_callback:
            log_callback("📊 Using 'Your Activity' node for data parsing")
        return your_activity
    
    # Fall back to 'Likes and Favorites'
    likes_and_favorites = data.get('Likes and Favorites', {})
    if likes_and_favorites:
        if log_callback:
            log_callback("📊 'Your Activity' node not found, falling back to 'Likes and Favorites' node")
        return likes_and_favorites
    
    # If neither exists, return empty dict
    if log_callback:
        log_callback("⚠️ Neither 'Your Activity' nor 'Likes and Favorites' nodes found in JSON")
    return {}


# Function to parse date string and check if it's after earliest date
def is_date_after_earliest(date_str, earliest_date):
    if not date_str or not earliest_date:
        return True
    
    try:
        from datetime import datetime
        # Extract just the date part (before the space) from TikTok format "YYYY-MM-DD HH:MM:SS"
        date_part = date_str.split(' ')[0] if ' ' in date_str else date_str
        
        # Parse the date part
        video_date = datetime.strptime(date_part, '%Y-%m-%d').date()
        result = video_date >= earliest_date
        return result
    except Exception as e:
        # If parsing fails, include the video
        return True



# Function to download video using yt-dlp
def download_video(video_url, download_folder, prefix, stop_event=None):
    if stop_event and stop_event.is_set():
        raise DownloadCancelled('Download cancelled before start')

    def _progress_hook(d):
        if stop_event and stop_event.is_set():
            raise DownloadCancelled('Download cancelled by user')

    ydl_opts = {
        # Output template for downloaded videos
        'outtmpl': path.join(download_folder, f"{prefix}%(id)s.%(ext)s"),
        # Specify the format to download: best available video and audio
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        'progress_hooks': [_progress_hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


# Function to check if a video is already downloaded
def is_video_downloaded(video_url, downloaded_videos, prefix):
    # Extract video_id from URL (assuming the last part of the path is the video id)
    video_id = video_url.strip('/').split('/')[-1]
    expected_filenames = [
        file for file in downloaded_videos
        if file.startswith(prefix)
        and (file.endswith(f"{video_id}.mp4")
             or file.endswith(f"{video_id}.m4a")
             or file.endswith(f"{video_id}.mp3"))
    ]
    return len(expected_filenames) > 0


# Function to get a set of already downloaded video filenames; creates folder if needed
def get_downloaded_videos(download_folder):
    downloaded_videos = set()
    try:
        makedirs(download_folder, exist_ok=True)
        for file_name in listdir(download_folder):
            downloaded_videos.add(file_name)
    except PermissionError as e:
        print(f"Warning: Permission denied accessing {download_folder}: {e}")
        print("Using empty download list - all videos will be re-downloaded")
    except Exception as e:
        print(f"Warning: Error accessing download folder {download_folder}: {e}")
        print("Using empty download list - all videos will be re-downloaded")
    return downloaded_videos


# Main processing function (with progress callback added)
def process_videos(json_file, download_folder, log_callback, progress_callback, detailed_progress_callback, download_faves, download_likes, download_shares, earliest_date=None, stop_event=None, max_concurrent_downloads=3, blocked_videos=None, failed_videos=None):
    # Attempt to load the JSON file
    try:
        data = load_json(json_file)
    except Exception as e:
        log_callback(f"Error loading JSON file: {e}")
        return 0, 0, 0, 0, 0, 0, 0, 0, []  # Return zero counts on error

    video_links = []

    # Get activity data with fallback logic
    activity_data = get_activity_data(data, log_callback)

    # Process favorite videos if selected
    if download_faves:
        favorite_videos = activity_data.get('Favorite Videos', {}).get('FavoriteVideoList', [])
        for video in favorite_videos:
            # Check if video date is after earliest date filter
            video_date = video.get('Date', '')
            if is_date_after_earliest(video_date, earliest_date):
                # Format date for filename prefix
                date = video_date.replace(':', '').replace(' ', '-').replace('/', '-')
                video_links.append((video['Link'], f"faved_{date}_" if date else "faved_"))

    # Process liked videos if selected
    if download_likes:
        liked_videos = activity_data.get('Like List', {}).get('ItemFavoriteList', [])
        for video in liked_videos:
            # Check if video date is after earliest date filter
            video_date = video.get('date', '')
            if is_date_after_earliest(video_date, earliest_date):
                # Format date for filename prefix
                date = video_date.replace(':', '').replace(' ', '-').replace('/', '-')
                video_links.append((video['link'], f"liked_{date}_" if date else "liked_"))

    # Process shared videos if selected
    if download_shares:
        shared_videos = activity_data.get('Share History', {}).get('ShareHistoryList', [])
        for video in shared_videos:
            # Check if video date is after earliest date filter
            video_date = video.get('Date', '')
            if is_date_after_earliest(video_date, earliest_date):
                # Format date for filename prefix
                date = video_date.replace(':', '').replace(' ', '-').replace('/', '-')
                video_links.append((video['Link'], f"shared_{date}_" if date else "shared_"))

    # Validate download folder and get existing videos
    try:
        downloaded_videos = get_downloaded_videos(download_folder)
        log_callback(f"📁 Download folder: {download_folder}")
        log_callback(f"📊 Found {len(downloaded_videos)} existing videos")
    except Exception as e:
        log_callback(f"❌ Error accessing download folder: {e}")
        log_callback("🔄 Using empty download list - all videos candidates")
        downloaded_videos = set()

    total_videos = len(video_links)
    if total_videos == 0:
        log_callback("No videos to download.")
        return 0, 0, 0, 0, 0, 0, 0, 0, []

    downloaded_count = 0
    blocked_count = 0
    failed_count = 0
    downloaded_faves = 0
    downloaded_likes = 0
    downloaded_shares = 0

    start_time = time.time()
    download_times = []
    processed_count = 0
    active_futures = {}
    stall_reported = False
    STALL_THRESHOLD = 60
    stop_event = stop_event or threading.Event()
    pending_tasks = []

    def emit_progress(context):
        elapsed_time = time.time() - start_time
        video_id = context['url'].strip('/').split('/')[-1]
        detailed_progress_callback({
            'current_video': min(context['index'], total_videos),
            'total_videos': total_videos,
            'current_url': context['url'],
            'video_id': video_id,
            'prefix': context['prefix'],
            'elapsed_time': elapsed_time,
            'downloaded_count': downloaded_count,
            'failed_count': failed_count
        })

    def update_progress_bar():
        if total_videos == 0:
            progress = 0
        else:
            progress = int((processed_count / total_videos) * 100)
        progress_callback(progress)

    def check_for_stall():
        nonlocal stall_reported
        if not active_futures:
            if stall_reported:
                stall_reported = False
            return
        current_time = time.time()
        stalled = any(
            info.get('start_time') is not None and current_time - info['start_time'] > STALL_THRESHOLD
            for info in active_futures.values()
        )
        if stalled and not stall_reported:
            stall_reported = True
            log_callback(f"⚠️ Download appears stalled (>{STALL_THRESHOLD}s)")
        elif not stalled and stall_reported:
            stall_reported = False
            log_callback("✅ Download resumed...")

    def download_task(url, prefix):
        start = time.time()
        try:
            download_video(url, download_folder, prefix, stop_event=stop_event)
            duration = time.time() - start
            return {'status': 'downloaded', 'duration': duration}
        except DownloadCancelled:
            return {'status': 'cancelled'}
        except Exception as exc:
            return {'status': 'error', 'error': str(exc)}

    def harvest_futures(block):
        nonlocal downloaded_count, processed_count, downloaded_faves, downloaded_likes, downloaded_shares, failed_count
        if not active_futures:
            return False
        timeout = None if block else 0
        done, _ = wait(list(active_futures.keys()), timeout=timeout, return_when=FIRST_COMPLETED)
        if not done:
            return False
        for future in done:
            context = active_futures.pop(future)
            result = {}
            try:
                result = future.result()
            except DownloadCancelled:
                result = {'status': 'cancelled'}
            except Exception as exc:
                result = {'status': 'error', 'error': str(exc)}

            status = result.get('status')
            if status == 'downloaded':
                duration = result.get('duration')
                if duration:
                    download_times.append(duration)
                downloaded_count += 1
                if "faved_" in context['prefix']:
                    downloaded_faves += 1
                elif "liked_" in context['prefix']:
                    downloaded_likes += 1
                elif "shared_" in context['prefix']:
                    downloaded_shares += 1
                log_callback(f"✅ Downloaded: {context['url']}")
            elif status == 'cancelled':
                log_callback(f"🛑 Cancelled: {context['url']}")
            else:
                error_message = result.get('error', 'Unknown error')
                url = context['url']
                failed_count += 1
                # Check if this is a blocked video error
                if 'IP address is blocked' in error_message:
                    log_callback(f"🚫 Blocked: {url} - IP address blocked")
                    # Add to blocked videos set and save to session
                    if blocked_videos is not None:
                        """Add a video URL to the blocked set and save to favesave_errors.json"""
                        blocked_videos.add(url)
                        save_session_data(download_folder, blocked_videos, failed_videos)

                else:
                    log_callback(f"❌ Failed to download {url} : {error_message}")
                    # Add to failed videos set and save to session
                    if failed_videos is not None:
                        """Add a video URL to the failed set and save to favesave_errors.json"""
                        failed_videos.add(url)
                        save_session_data(download_folder, blocked_videos, failed_videos)


            processed_count += 1
            check_for_stall()
            emit_progress(context)
            update_progress_bar()
        return True

    for index, (url, prefix) in enumerate(video_links, start=1):
        if stop_event.is_set():
            break
        context = {'index': index, 'url': url, 'prefix': prefix}
        
        # Check if video is blocked
        if blocked_videos and url in blocked_videos:
            log_callback(f"🎥 Processing Video {index} of {total_videos}")
            log_callback(f"🚫 Skipping blocked video: {url}")
            failed_count += 1
            processed_count += 1
            emit_progress(context)
            update_progress_bar()
            QCoreApplication.processEvents()
            continue

        if failed_videos and url in failed_videos:
            log_callback(f"🎥 Processing Video {index} of {total_videos}")
            log_callback(f"❌ Skipping failed video: {url}")
            failed_count += 1
            processed_count += 1
            emit_progress(context)
            update_progress_bar()
            QCoreApplication.processEvents()
            continue

        if is_video_downloaded(url, downloaded_videos, prefix):
            log_callback(f"🎥 Processing Video {index} of {total_videos}")
            log_callback(f"Already downloaded: {url}")
            downloaded_count += 1
            if "faved_" in prefix:
                downloaded_faves += 1
            elif "liked_" in prefix:
                downloaded_likes += 1
            elif "shared_" in prefix:
                downloaded_shares += 1
            processed_count += 1
            emit_progress(context)
            update_progress_bar()
            QCoreApplication.processEvents()
        else:
            pending_tasks.append(context)

    if stop_event.is_set():
        update_progress_bar()
        return (
            total_videos,
            downloaded_count,
            failed_count,
            downloaded_faves,
            downloaded_likes,
            downloaded_shares,
            video_links
        )

    with ThreadPoolExecutor(max_workers=max_concurrent_downloads) as executor:
        for context in pending_tasks:
            if stop_event.is_set():
                log_callback("Cancellation requested - stopping new downloads")
                break

            emit_progress(context)
            log_callback(f"🎥 Processing Video {context['index']} of {total_videos}")

            url = context['url']
            prefix = context['prefix']
            log_callback(f"Downloading: {url}")

            while len(active_futures) >= max_concurrent_downloads and not stop_event.is_set():
                if not harvest_futures(block=True):
                    break
                check_for_stall()

            if stop_event.is_set():
                break

            future = executor.submit(download_task, url, prefix)
            context['start_time'] = time.time()
            active_futures[future] = context
            check_for_stall()

            while harvest_futures(block=False):
                check_for_stall()

        if stop_event.is_set():
            for future in list(active_futures.keys()):
                future.cancel()

        while active_futures:
            harvest_futures(block=True)
            check_for_stall()

    update_progress_bar()

    return (
        total_videos,
        downloaded_count,
        blocked_count,
        failed_count,
        downloaded_faves,
        downloaded_likes,
        downloaded_shares,
        video_links
    )


# Worker Thread to handle video processing in the background
class VideoDownloadWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    detailed_progress_signal = pyqtSignal(dict)  # New signal for detailed progress info

    def __init__(self, json_file, download_folder, download_faves, download_likes, download_shares, earliest_date=None, blocked_videos=None, failed_videos=None):
        super().__init__()
        self.json_file = json_file
        self.download_folder = download_folder
        self.download_faves = download_faves
        self.download_likes = download_likes
        self.download_shares = download_shares
        self.earliest_date = earliest_date
        self.blocked_videos = blocked_videos
        self.failed_videos = failed_videos
        self.total_videos = 0
        self.downloaded_videos = 0
        self.blocked_videos_count = 0
        self.failed_videos_count = 0
        self.downloaded_faves = 0
        self.downloaded_likes = 0
        self.downloaded_shares = 0
        self.start_time = None
        self.current_video_url = ""
        self.current_video_index = 0
        self.stop_event = threading.Event()
        self.max_concurrent_downloads = 3


    def run(self):
        self.stop_event.clear()
        results = process_videos(
            self.json_file,
            self.download_folder,
            self.log_signal.emit,
            self.progress_signal.emit,
            self.detailed_progress_signal.emit,
            self.download_faves,
            self.download_likes,
            self.download_shares,
            self.earliest_date,
            stop_event=self.stop_event,
            max_concurrent_downloads=self.max_concurrent_downloads,
            blocked_videos=self.blocked_videos,
            failed_videos=self.failed_videos
        )
        (
            self.total_videos,
            self.downloaded_videos,
            self.blocked_videos_count,
            self.failed_videos_count,
            self.downloaded_faves,
            self.downloaded_likes,
            self.downloaded_shares,
            _
        ) = results

    def request_cancel(self):
        self.stop_event.set()


# PyQt6 Main Window for the Video Downloader Application
class VideoDownloaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FaveSave - TikTok Video Downloader -v1.2.0")
        self.setGeometry(100, 100, 600, 700)

        self.json_file = None
        self.download_folder = ""
        self.worker = None
        self.progress_bar = None
        self._cached_json_data = None
        self._cached_json_file = None
        self.is_downloading = False
        self.was_cancelled = False
        
        # Session tracking for blocked and failed videos (will be loaded when download folder is set)
        self.blocked_videos = set()
        self.failed_videos = set()
        
        # Watchdog system
        self.watchdog_timer = None
        self.last_heartbeat = 0
        self.heartbeat_count = 0
        self.watchdog_timeout = 30  # 30 seconds timeout
        self.max_hang_duration = 120  # 2 minutes before showing recovery dialog

        # Initialize UI components
        self.init_ui()
        
        # Initialize watchdog system
        self.init_watchdog()
        
        # Load saved settings
        self.load_settings()
        
        # Load session data if download folder is already set
        if self.download_folder:
            self.load_session_data()

    def load_session_data(self):
        """Load blocked and failed videos from session file in download folder"""
        if self.download_folder:
            self.blocked_videos, self.failed_videos = load_session_data(self.download_folder)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Logo label
        logo_label = QLabel()
        logo_pixmap = QPixmap(logo_path)  # Load the logo image
        logo_label.setPixmap(logo_pixmap)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo_label)

        # Title label
        title_label = QLabel("Download All of Your Favorite/Liked TikTok Videos")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(title_label)

        # JSON Path label with instructions and link
        self.json_label = QLabel("📂 Select your JSON file from your exported TikTok data ( Click <a href='https://github.com/joeycato/tiktok-favesave'>here</a> for more details ):")
        self.json_label.setOpenExternalLinks(True)
        layout.addWidget(self.json_label)

        # Button to select JSON file
        self.json_button = QPushButton("No JSON file selected ( Click here to add...)")
        self.json_button.clicked.connect(self.set_json_path)
        self.json_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.json_button)

        # Output Folder label and button
        self.output_folder_label = QLabel("📂 Set Download Folder ( where you want to save your videos ):")
        layout.addWidget(self.output_folder_label)

        self.output_folder_button = QPushButton("( Defaults to parent directory of selected JSON file above )")
        self.output_folder_button.clicked.connect(self.set_output_folder)
        self.output_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.output_folder_button)

        # Label for download options
        download_options_label = QLabel("🎥 Which videos do you wish to download?")
        download_options_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(download_options_label)

        # Checkbox for favorite videos
        self.faves_checkbox = QCheckBox("🔖 Favorited")
        self.faves_checkbox.setChecked(True)
        self.faves_checkbox.toggled.connect(self.update_filter_counts)  # Update counts when toggled
        layout.addWidget(self.faves_checkbox)

        # Checkbox for liked videos
        self.likes_checkbox = QCheckBox("❤️ Liked")
        self.likes_checkbox.setChecked(True)
        self.likes_checkbox.toggled.connect(self.update_filter_counts)  # Update counts when toggled
        layout.addWidget(self.likes_checkbox)

        # Checkbox for shared videos
        self.shares_checkbox = QCheckBox("⬆️ Shared")
        self.shares_checkbox.setChecked(True)
        self.shares_checkbox.toggled.connect(self.update_filter_counts)  # Update counts when toggled
        layout.addWidget(self.shares_checkbox)


        # Advanced settings section
        advanced_label = QLabel("⚙️ Advanced Settings:")
        advanced_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(advanced_label)

        # Create a container widget for advanced settings
        self.advanced_settings_widget = QWidget()
        self.advanced_settings_layout = QVBoxLayout(self.advanced_settings_widget)
        self.advanced_settings_layout.setContentsMargins(10, 5, 10, 15)  # Add margins for spacing
        self.advanced_settings_layout.setSpacing(8)  # Add spacing between elements

        # Concurrent downloads setting
        concurrent_layout = QHBoxLayout()
        concurrent_label = QLabel("🔄 Max Concurrent Downloads:")
        concurrent_label.setStyleSheet("font-size: 12px;")
        concurrent_layout.addWidget(concurrent_label)
        
        self.concurrent_downloads_spinner = QSpinBox()
        self.concurrent_downloads_spinner.setMinimum(1)
        self.concurrent_downloads_spinner.setMaximum(10)
        self.concurrent_downloads_spinner.setValue(1)  # Default to 1
        self.concurrent_downloads_spinner.setToolTip("Number of videos to download simultaneously (1-10)")
        self.concurrent_downloads_spinner.valueChanged.connect(self.on_concurrent_downloads_changed)
        concurrent_layout.addWidget(self.concurrent_downloads_spinner)
        
        concurrent_layout.addStretch()  # Push controls to the left
        self.advanced_settings_layout.addLayout(concurrent_layout)

        # Retry previous failures checkbox
        self.retry_failures_checkbox = QCheckBox("🔄 Retry failed downloads on subsequent runs")
        self.retry_failures_checkbox.setChecked(False)  # Default to unchecked
        self.retry_failures_checkbox.setStyleSheet("font-size: 12px;")
        self.retry_failures_checkbox.toggled.connect(self.save_settings)  # Save settings when toggled
        self.advanced_settings_layout.addWidget(self.retry_failures_checkbox)

        # Date filter setting
        self.enable_date_filter = QCheckBox("🔍 Filter by earliest date - only videos from selected date onwards considered")
        self.enable_date_filter.setChecked(False)  # Default to download all
        self.enable_date_filter.toggled.connect(self.toggle_date_filter)
        self.advanced_settings_layout.addWidget(self.enable_date_filter)

        # Date input for earliest date filter (initially hidden)
        self.date_filter = QDateEdit()
        self.date_filter.setDate(QDate.currentDate().addYears(-1))  # Default to 1 year ago
        self.date_filter.setCalendarPopup(True)
        self.date_filter.setDisplayFormat("yyyy-MM-dd")
        self.date_filter.setVisible(False)  # Initially hidden
        self.date_filter.dateChanged.connect(self.on_date_filter_changed)  # Update counts and save when date changes
        self.advanced_settings_layout.addWidget(self.date_filter)

        # Help text for date filtering
        self.date_help_label = QLabel("💡 Leave unchecked to download all videos, or check to filter by date")
        self.date_help_label.setStyleSheet("color: #666; font-size: 12px;")
        self.date_help_label.setVisible(False)  # Initially hidden
        self.advanced_settings_layout.addWidget(self.date_help_label)

        # Add the advanced settings container to the main layout
        layout.addWidget(self.advanced_settings_widget)

        # Text area for logging messages
        self.description = QTextBrowser()
        self.description.setPlaceholderText("Logs will appear here...")
        self.description.setReadOnly(True)
        self.description.setOpenExternalLinks(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.description.setMinimumHeight(150)  # Set minimum height for better log visibility
        layout.addWidget(self.description)

        # Progress bar for download progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #ddd;
                border-radius: 8px;
                text-align: center;
                font-weight: bold;
                font-size: 14px;
                height: 25px;
                background-color: #f0f0f0;
                color: #333;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4CAF50, stop:1 #45a049);
                border-radius: 6px;
            }
        """)
        layout.addWidget(self.progress_bar)
        
        # Detailed progress information
        self.progress_info_label = QLabel("Select your JSON file and configure settings above")
        self.progress_info_label.setStyleSheet("""
            color: #333; 
            font-size: 14px; 
            font-weight: bold;
            padding: 8px;
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 6px;
        """)
        self.progress_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress_info_label)
        
        # Spacer and download controls
        spacer = QSpacerItem(0, 20, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        layout.addItem(spacer)
        
        # Download status and controls container
        self.download_controls_widget = QWidget()
        self.download_controls_layout = QVBoxLayout(self.download_controls_widget)
        self.download_controls_layout.setContentsMargins(0, 0, 0, 0)
                
        # Start/Cancel button
        self.start_button = QPushButton("Start Download")
        self.start_button.clicked.connect(self.start_download)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 10px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        self.download_controls_layout.addWidget(self.start_button)
        
        # Cancel button (initially hidden)
        self.cancel_button = QPushButton("Cancel Download")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setVisible(False)
        self.cancel_button.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 10px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #da190b;
            }
        """)
        self.download_controls_layout.addWidget(self.cancel_button)
        
        layout.addWidget(self.download_controls_widget)

        # Donation link label
        donation_label = QLabel('☕️ <a href="https://buymeacoffee.com/myretrotvs">Buy me a coffee</a>')
        donation_label.setOpenExternalLinks(True)
        donation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(donation_label)


    # Get cached JSON data, loading from file only if necessary
    def get_cached_json_data(self):
        if not self.json_file:
            return None
        
        # Check if we need to reload the JSON file
        if (self._cached_json_data is None or 
            self._cached_json_file != self.json_file):
            try:
                self._cached_json_data = load_json(self.json_file)
                self._cached_json_file = self.json_file
            except Exception:
                self._cached_json_data = None
                self._cached_json_file = None
        
        return self._cached_json_data

    # Calculate how many videos match current filter settings
    def calculate_filtered_counts(self):
        if not self.json_file:
            return 0, 0, 0, 0  # faves, likes, shares, total
        
        try:
            data = self.get_cached_json_data()
            if data is None:
                return 0, 0, 0, 0
            
            # Get earliest date if filtering is enabled
            earliest_date = None
            if self.enable_date_filter.isChecked():
                qdate = self.date_filter.date()
                from datetime import date
                earliest_date = date(qdate.year(), qdate.month(), qdate.day())
            
            # Get activity data with fallback logic
            activity_data = get_activity_data(data, None)  # No logging for count calculation
            
            # Count favorite videos
            favorite_videos = activity_data.get('Favorite Videos', {}).get('FavoriteVideoList', [])
            faves_count = 0
            if self.faves_checkbox.isChecked():
                for video in favorite_videos:
                    video_date = video.get('Date', '')
                    if is_date_after_earliest(video_date, earliest_date):  # No logging for count calculation
                        faves_count += 1
            
            # Count liked videos
            liked_videos = activity_data.get('Like List', {}).get('ItemFavoriteList', [])
            likes_count = 0
            if self.likes_checkbox.isChecked():
                for video in liked_videos:
                    video_date = video.get('date', '')
                    if is_date_after_earliest(video_date, earliest_date):  # No logging for count calculation
                        likes_count += 1
            
            # Count shared videos
            shared_videos = activity_data.get('Share History', {}).get('ShareHistoryList', [])
            shares_count = 0
            if self.shares_checkbox.isChecked():
                for video in shared_videos:
                    video_date = video.get('date', '')
                    if is_date_after_earliest(video_date, earliest_date):  # No logging for count calculation
                        shares_count += 1
            
            total_count = faves_count + likes_count + shares_count
            return faves_count, likes_count, shares_count, total_count
            
        except Exception:
            return 0, 0, 0, 0

    # Update filter counts and checkbox labels
    def update_filter_counts(self):
        faves_count, likes_count, shares_count, total_count = self.calculate_filtered_counts()
        
        # Update the date filter checkbox label with total count
        if self.enable_date_filter.isChecked():
            self.enable_date_filter.setText(f"🔍 Filter by earliest date ({total_count} candidates) - only videos from selected date onwards considered")
        else:
            self.enable_date_filter.setText("🔍 Filter by earliest date - only videos from selected date onwards considered")
        
        # Update individual checkbox labels
        self.faves_checkbox.setText(f"🔖 Favorited ({faves_count} candidates)")
        self.likes_checkbox.setText(f"❤️ Liked ({likes_count} candidates)")
        self.shares_checkbox.setText(f"⬆️ Shared ({shares_count} candidates)")

    # Toggle date filter visibility
    def toggle_date_filter(self, checked, log_message=True):
        self.date_filter.setVisible(checked)
        self.date_help_label.setVisible(checked)
        self.update_filter_counts()  # Update counts when toggling
        
        # Save settings when date filter is toggled
        self.save_settings()
    
    # Handle date filter value changes
    def on_date_filter_changed(self):
        """Called when the date filter value changes"""
        self.update_filter_counts()  # Update counts when date changes
        self.save_settings()  # Save settings when date changes

    # Handle concurrent downloads value changes
    def on_concurrent_downloads_changed(self):
        """Called when the concurrent downloads value changes"""
        self.save_settings()  # Save settings when value changes

    # Append message to the log area
    def log_message(self, message):
        text = str(message)
        lines = text.splitlines() or ['']
        for line in lines:
            if 'http' in line.lower():
                formatted_message = make_links_clickable(line)
            else:
                formatted_message = html.escape(line)
            # Use append for consistency; Qt wraps each call in its own block.
            self.description.append(formatted_message if formatted_message else '&nbsp;')
        # Scroll to bottom to show latest message
        cursor = self.description.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.description.setTextCursor(cursor)
        self.description.ensureCursorVisible()
        # Update heartbeat for watchdog
        self.update_heartbeat()

    # Update the checkbox labels with counts from the JSON file
    def update_checkbox_labels(self):
        if self.json_file:
            try:
                # Use the new counting system that respects current filter settings
                self.update_filter_counts()
            except Exception as e:
                self.log_message(f"Error loading JSON file for video count: {e}")

    # Open a file dialog to select the JSON file
    def set_json_path(self):
        # Default to current JSON file's directory if it exists, otherwise use current directory
        default_dir = ""
        if self.json_file and path.exists(self.json_file):
            default_dir = path.dirname(self.json_file)
        elif not default_dir:
            default_dir = path.abspath(".")
            
        json_file, _ = QFileDialog.getOpenFileName(
            self, "Select JSON File", default_dir, "JSON Files (*.json);;All Files (*)"
        )
        if json_file:
            self.json_file = json_file
            # Clear cache when new JSON file is selected
            self._cached_json_data = None
            self._cached_json_file = None
            self.update_checkbox_labels()
            self.json_button.setText(f"JSON Path: {json_file}")
            self.log_message(f"JSON path set to: {json_file}")

            # Update download_folder to the parent directory of the JSON file
            self.download_folder = path.join(path.dirname(json_file), "downloaded_videos")
            self.output_folder_label.setText("📁 Set Output Folder:")
            self.output_folder_button.setText(self.download_folder)
            self.log_message(f"Output folder set to: {self.download_folder}")
            
            # Load session data for the new download folder
            self.load_session_data()
            
            # Save settings
            self.save_settings()

    # Open a file dialog to select the output folder
    def set_output_folder(self):
        # Default to current download folder if it exists, otherwise use current directory
        default_dir = ""
        if self.download_folder and path.exists(self.download_folder):
            default_dir = self.download_folder
        elif not default_dir:
            default_dir = path.abspath(".")
            
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder", default_dir)
        if folder:
            self.download_folder = folder
            self.output_folder_label.setText("📁 Set Output Folder:")
            self.output_folder_button.setText(folder)
            self.log_message(f"Output folder set to: {folder}")
            
            # Load session data for the new download folder
            self.load_session_data()
            
            # Save settings
            self.save_settings()

    # Start the download process
    def start_download(self):
        if not self.json_file:
            QMessageBox.warning(self, "Warning", "Please set a JSON path first.")
            return
        
        if self.is_downloading:
            QMessageBox.warning(self, "Warning", "Download is already in progress. Please cancel the current download first.")
            return
        
        # Validate download folder - if not set but JSON file is available, set it automatically
        if not self.download_folder:
            if self.json_file:
                # Auto-set download folder to parent directory of JSON file
                self.download_folder = path.join(path.dirname(self.json_file), "downloaded_videos")
                self.output_folder_label.setText("📁 Set Output Folder:")
                self.output_folder_button.setText(self.download_folder)
                self.log_message(f"🔧 Auto-set download folder to: {self.download_folder}")
                # Load session data for the new download folder
                self.load_session_data()
            else:
                QMessageBox.warning(self, "Warning", "Please set a download folder first.")
                return
        
        # Check if download folder is accessible
        try:
            from os import access, W_OK
            if not access(self.download_folder, W_OK):
                QMessageBox.warning(self, "Warning", f"Cannot write to download folder: {self.download_folder}\nPlease choose a different folder.")
                return
        except Exception as e:
            QMessageBox.warning(self, "Warning", f"Error validating download folder: {e}\nPlease choose a different folder.")
            return

        download_faves = self.faves_checkbox.isChecked()
        download_likes = self.likes_checkbox.isChecked()
        download_shares = self.shares_checkbox.isChecked()

        if not (download_faves or download_likes or download_shares):
            QMessageBox.warning(self, "Warning", "Select at least one category (Favorited, Liked, or Shared) to download.")
            return
        
        # Get date filter if enabled
        if self.enable_date_filter.isChecked():
            qdate = self.date_filter.date()
            from datetime import date
            earliest_date = date(qdate.year(), qdate.month(), qdate.day())
        else:
            earliest_date = None

        # Check if user wants to retry previous failures
        if self.retry_failures_checkbox.isChecked():
            try:
                # Delete the favesave_errors.json file if it exists
                session_file = path.join(self.download_folder, "favesave_errors.json")
                if path.exists(session_file):
                    os.remove(session_file)
                    self.log_message(f"🗑️ Cleared previous failures - deleted: {session_file}")
                else:
                    self.log_message("ℹ️ No previous failures found to clear")
                
                # Reset the sets
                self.blocked_videos = set()
                self.failed_videos = set()
                
                self.log_message("✅ Previous failures cleared - blocked and failed videos will be retried")
                
            except Exception as e:
                self.log_message(f"❌ Error clearing previous failures: {e}")
                QMessageBox.warning(self, "Error", f"Could not clear previous failures: {e}")

        self.log_message(f"Selected JSON File: {self.json_file}")
        self.log_message(f"Selected Output Folder: {self.download_folder}")
        if earliest_date:
            self.log_message(f"Earliest Date Filter: {earliest_date}")
        else:
            self.log_message("No date filter - downloading all videos")

        # Reset the progress bar and progress info
        self.progress_bar.setValue(0)
        
        # Calculate total videos for better status message
        faves_count, likes_count, shares_count, total_count = self.calculate_filtered_counts()
        if total_count > 0:
            self.progress_info_label.setText(f"🚀 Starting download of {total_count:,} videos...")
        else:
            self.progress_info_label.setText("🚀 Starting download...")
        
        # Reset cancelled flag when starting new download
        self.was_cancelled = False
        
        # Update UI state to show downloading
        self.update_download_ui_state(True)

        # Create a worker thread to process downloads without freezing the UI
        max_concurrent = self.concurrent_downloads_spinner.value()
        self.worker = VideoDownloadWorker(self.json_file, self.download_folder, download_faves, download_likes, download_shares, earliest_date, self.blocked_videos, self.failed_videos)
        self.worker.max_concurrent_downloads = max_concurrent
        self.worker.log_signal.connect(self.log_message)
        self.worker.progress_signal.connect(self.update_progress_bar)
        self.worker.detailed_progress_signal.connect(self.update_detailed_progress)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    # Called when the worker thread finishes processing
    def on_worker_finished(self):
        if not self.worker:
            self.update_download_ui_state(False)
            return

        # Immediately disable watchdog checks to prevent false unresponsive warnings
        self.is_downloading = False

        cancel_requested = self.worker.stop_event.is_set() or self.was_cancelled

        if cancel_requested:
            self.log_message("❌ Download cancelled by user")
            self.progress_info_label.setText("⏸️ Download cancelled - click Resume to continue")
        else:
            # Calculate completion statistics
            total_processed = self.worker.downloaded_videos + self.worker.failed_videos_count + self.worker.blocked_videos_count
            success_rate = (self.worker.downloaded_videos / total_processed * 100) if total_processed > 0 else 0
            
            # Cap success rate at 100% to prevent display issues
            success_rate = min(success_rate, 100.0)
            
            self.log_message(f"🎉 Download completed! {self.worker.total_videos:,} total videos processed")
            self.log_message(f"✅ Successfully downloaded: {self.worker.downloaded_videos:,} videos")
            self.log_message(f"🔖 Favorite Videos: {self.worker.downloaded_faves:,} downloaded")
            self.log_message(f"❤️ Liked Videos: {self.worker.downloaded_likes:,} downloaded")
            
            if self.worker.blocked_videos_count > 0:
                self.log_message(f"🚫 Blocked Videos: {self.worker.blocked_videos_count:,} (IP address blocked)")
            if self.worker.failed_videos_count > 0:
                self.log_message(f"❌ Failed Videos: {self.worker.failed_videos_count:,} (download errors)")
            
            # Update progress info with completion summary
            self.progress_info_label.setText(
                f"🎉 Download Complete! {self.worker.downloaded_videos:,} videos downloaded "
                f"({success_rate:.1f}% success rate)"
            )
            
            self.start_button.setText("🎉 Done! (Click to start new download)")
            self.was_cancelled = False

        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel Download")

        # Reset watchdog to prevent false unresponsive warnings after download completion
        self.update_heartbeat()
        
        # Reset UI state
        self.update_download_ui_state(False)
        self.worker = None

    # Update the progress bar value
    def update_progress_bar(self, value):
        self.progress_bar.setValue(value)
        # Update heartbeat for watchdog
        self.update_heartbeat()
    
    # Update UI state for download status
    def update_download_ui_state(self, is_downloading):
        self.is_downloading = is_downloading
        if is_downloading:
            self.start_button.setVisible(False)
            self.cancel_button.setVisible(True)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel Download")
            
            # Disable controls that shouldn't be changed during download
            self.json_button.setEnabled(False)
            self.output_folder_button.setEnabled(False)
            self.faves_checkbox.setEnabled(False)
            self.likes_checkbox.setEnabled(False)
            self.shares_checkbox.setEnabled(False)
            self.enable_date_filter.setEnabled(False)
            self.date_filter.setEnabled(False)
            self.concurrent_downloads_spinner.setEnabled(False)
            self.retry_failures_checkbox.setEnabled(False)
        else:
            # Update button text based on whether download was cancelled
            if self.was_cancelled:
                self.start_button.setText("Resume Download")
            else:
                self.start_button.setText("Start Download")
            self.start_button.setVisible(True)
            self.cancel_button.setVisible(False)
            
            # Re-enable controls when download is not active
            self.json_button.setEnabled(True)
            self.output_folder_button.setEnabled(True)
            self.faves_checkbox.setEnabled(True)
            self.likes_checkbox.setEnabled(True)
            self.shares_checkbox.setEnabled(True)
            self.enable_date_filter.setEnabled(True)
            self.date_filter.setEnabled(True)
            self.concurrent_downloads_spinner.setEnabled(True)
            self.retry_failures_checkbox.setEnabled(True)
    
    # Cancel the download process
    def cancel_download(self):
        if self.worker and self.worker.isRunning():
            self.log_message("🛑 Cancelling download...")
            self.was_cancelled = True  # Mark as cancelled
            self.worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling...")
            self.progress_info_label.setText("⏸️ Cancelling downloads... Please wait...")
            
            # Reset watchdog to prevent false unresponsive warnings after cancellation
            self.update_heartbeat()
    
    # Update detailed progress information
    def update_detailed_progress(self, progress_info):
        current = progress_info['current_video']
        total = progress_info['total_videos']
        downloaded = progress_info['downloaded_count']
        failed = progress_info['failed_count']
        elapsed = progress_info['elapsed_time']
        
        # Format elapsed time
        elapsed_minutes = int(elapsed // 60)
        elapsed_seconds = int(elapsed % 60)
        elapsed_str = f"{elapsed_minutes:02d}:{elapsed_seconds:02d}"
        
        # Calculate success rate based on current session downloads vs current session processed
        current_session_processed = current
        current_session_downloaded = downloaded
        success_rate = (current_session_downloaded / current_session_processed * 100) if current_session_processed > 0 else 0
        
        # Cap success rate at 100% to prevent display issues
        success_rate = min(success_rate, 100.0)
        remaining_videos = total - current
        avg_time_per_video = elapsed / current if current > 0 else 0
        estimated_remaining_time = remaining_videos * avg_time_per_video
               
        # Update progress info label with enhanced information
        self.progress_info_label.setText(
            f"📊 Progress: {current:,}/{total:,} videos | "
            f"✅ Downloaded: {downloaded:,} | ❌ Failed: {failed:,} | "
            f"⏱️ Elapsed: {elapsed_str}"
        )
                       
        # Update heartbeat for watchdog
        self.update_heartbeat()
    
    # Watchdog system methods
    def init_watchdog(self):
        """Initialize the watchdog timer system"""
        self.watchdog_timer = QTimer()
        self.watchdog_timer.timeout.connect(self.watchdog_check)
        self.watchdog_timer.start(5000)  # Check every 5 seconds
        self.update_heartbeat()
    
    def update_heartbeat(self):
        """Update the heartbeat timestamp - call this from active operations"""
        self.last_heartbeat = time.time()
        self.heartbeat_count += 1
    
    def watchdog_check(self):
        """Check if the application is responsive"""
        current_time = time.time()
        
        # Only check for unresponsiveness during active downloads
        # Skip watchdog checks when download is cancelled or not running
        if not self.is_downloading or self.was_cancelled:
            return
            
        # Check if we've been unresponsive for too long
        if current_time - self.last_heartbeat > self.watchdog_timeout:
            self.handle_unresponsive_app()
    
    def handle_unresponsive_app(self):
        """Handle when the app becomes unresponsive"""
        hang_duration = time.time() - self.last_heartbeat
        
        if hang_duration > self.max_hang_duration:
            # Show recovery dialog
            self.show_recovery_dialog(hang_duration)
        # Removed unresponsive log message to avoid false positives during idle periods
    
    def show_recovery_dialog(self, hang_duration):
        """Show recovery dialog when app is unresponsive"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
        
        dialog = QDialog(self)
        dialog.setWindowTitle("App Unresponsive - Recovery Options")
        dialog.setModal(True)
        dialog.resize(400, 200)
        
        layout = QVBoxLayout(dialog)
        
        # Warning message
        warning_label = QLabel(
            f"⚠️ The application has been unresponsive for {int(hang_duration)} seconds.\n\n"
            "This may be due to a network issue, large file download, or system resource constraints.\n\n"
            "Choose an action:"
        )
        warning_label.setWordWrap(True)
        warning_label.setStyleSheet("color: #d32f2f; font-weight: bold;")
        layout.addWidget(warning_label)
        
        # Button layout
        button_layout = QHBoxLayout()
        
        # Wait button
        wait_button = QPushButton("Wait & Continue")
        wait_button.clicked.connect(lambda: self.handle_recovery_choice("wait", dialog))
        wait_button.setStyleSheet("background-color: #4CAF50; color: white; padding: 8px;")
        button_layout.addWidget(wait_button)
        
        # Cancel download button
        cancel_button = QPushButton("Cancel Download")
        cancel_button.clicked.connect(lambda: self.handle_recovery_choice("cancel", dialog))
        cancel_button.setStyleSheet("background-color: #f44336; color: white; padding: 8px;")
        button_layout.addWidget(cancel_button)
        
        # Force quit button
        quit_button = QPushButton("Force Quit")
        quit_button.clicked.connect(lambda: self.handle_recovery_choice("quit", dialog))
        quit_button.setStyleSheet("background-color: #ff9800; color: white; padding: 8px;")
        button_layout.addWidget(quit_button)
        
        layout.addLayout(button_layout)
        
        # Show dialog
        dialog.exec()
    
    def handle_recovery_choice(self, choice, dialog):
        """Handle user's recovery choice"""
        dialog.accept()
        
        if choice == "wait":
            self.log_message("⏳ Continuing to wait - monitoring will resume...")
            self.update_heartbeat()  # Reset heartbeat
        elif choice == "cancel":
            self.log_message("🛑 User requested download cancellation due to unresponsiveness")
            if self.is_downloading:
                self.cancel_download()
        elif choice == "quit":
            self.log_message("💥 User requested force quit due to unresponsiveness")
            QCoreApplication.quit()
    
    # Settings persistence methods
    def get_settings_file_path(self):
        """Get the path to the settings file"""
        settings_dir = os.path.expanduser("~/.favesave")
        os.makedirs(settings_dir, exist_ok=True)
        return os.path.join(settings_dir, "settings.json")
    
    def load_settings(self):
        """Load saved settings from file"""
        try:
            settings_file = self.get_settings_file_path()
            if os.path.exists(settings_file):
                with open(settings_file, 'r', encoding='utf-8') as f:
                    settings = json_load(f)
                
                # Restore JSON file path
                if 'json_file' in settings and settings['json_file']:
                    json_path = settings['json_file']
                    if os.path.exists(json_path):
                        self.json_file = json_path
                        self.json_button.setText(f"JSON Path: {json_path}")
                        self.update_checkbox_labels()
                
                # Restore download folder
                if 'download_folder' in settings and settings['download_folder']:
                    download_path = settings['download_folder']
                    if os.path.exists(download_path):
                        self.download_folder = download_path
                        self.output_folder_label.setText("📁 Set Output Folder:")
                        self.output_folder_button.setText(download_path)
                
                # Auto-set download folder if JSON file is loaded but download folder is not set or doesn't exist
                if self.json_file and not self.download_folder:
                    parent_dir = os.path.dirname(self.json_file)
                    self.download_folder = parent_dir
                    self.output_folder_label.setText("📁 Set Output Folder:")
                    self.output_folder_button.setText(self.download_folder)
                    self.log_message(f"🔧 Auto-set download folder to parent directory: {self.download_folder}")
                    # Load session data for the new download folder
                    self.load_session_data()
                    # Save the auto-set download folder
                    self.save_settings()
                
                # Restore date filter settings
                if 'date_filter_enabled' in settings:
                    self.enable_date_filter.setChecked(settings['date_filter_enabled'])
                    self.toggle_date_filter(settings['date_filter_enabled'], log_message=False)
                    
                if 'date_filter_value' in settings and settings['date_filter_value']:
                    try:
                        from datetime import datetime
                        saved_date = datetime.fromisoformat(settings['date_filter_value']).date()
                        qdate = QDate(saved_date.year, saved_date.month, saved_date.day)
                        self.date_filter.setDate(qdate)
                    except Exception as e:
                        self.log_message(f"⚠️ Could not restore date filter: {e}")
                
                # Restore concurrent downloads setting
                if 'concurrent_downloads' in settings:
                    self.concurrent_downloads_spinner.setValue(settings['concurrent_downloads'])
                
                # Restore retry failures setting
                if 'retry_failures' in settings:
                    self.retry_failures_checkbox.setChecked(settings['retry_failures'])
                
                self.log_message("⚙️ Settings restored from previous session")
        except Exception as e:
            self.log_message(f"⚠️ Could not load settings: {e}")
    
    def save_settings(self):
        """Save current settings to file"""
        try:
            # Get current date filter value
            date_filter_value = None
            if self.enable_date_filter.isChecked():
                qdate = self.date_filter.date()
                from datetime import date
                selected_date = date(qdate.year(), qdate.month(), qdate.day())
                date_filter_value = selected_date.isoformat()
            
            settings = {
                'json_file': self.json_file if self.json_file else '',
                'download_folder': self.download_folder if self.download_folder else '',
                'date_filter_enabled': self.enable_date_filter.isChecked(),
                'date_filter_value': date_filter_value,
                'concurrent_downloads': self.concurrent_downloads_spinner.value(),
                'retry_failures': self.retry_failures_checkbox.isChecked()
            }
            
            settings_file = self.get_settings_file_path()
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            self.log_message(f"⚠️ Could not save settings: {e}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.log_message("👋 Exiting - cancelling active downloads...")
            self.worker.request_cancel()
            self.worker.wait()
        super().closeEvent(event)


# Run the application
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoDownloaderApp()
    window.show()
    sys.exit(app.exec())
