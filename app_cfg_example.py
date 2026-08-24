api_1 = "api_key_1"
api_2 = "api_key_2"
api_3 = "api_key_3"

apikeys = [api_1, api_2, api_3]
account_names = ["api_name_1", "api_name_2", "api_name_3"]

path_to_save = "/your/path/goes/here"

# Operational state shared read-only with CMDownloaderUI.
state_database_path = "state/cm_downloader.db"

# Start the next ClickMeeting check this many seconds after the previous run ends.
run_interval_seconds = 300

# Start conservatively; increase only after observing network/storage behaviour.
max_download_workers = 2
