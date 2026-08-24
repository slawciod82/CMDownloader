CMDownloader
============

CMDownloader downloads recordings from ClickMeeting accounts and saves them locally.
After a recording is downloaded successfully and its file size matches the size reported by ClickMeeting, the recording is deleted from ClickMeeting.

Configuration
-------------

1. Copy app_cfg_example.py to app_cfg.py.
2. Enter your ClickMeeting API keys in app_cfg.py.
3. Keep account_names in the same order as apikeys.
4. Set path_to_save to the directory where recordings should be stored.

Example:

    api_1 = "your_api_key"
    apikeys = [api_1]
    account_names = ["My ClickMeeting account"]
    path_to_save = "/path/to/recordings"

Requirements
------------

- Python 3.9 or newer
- requests
- system timezone data for Europe/Warsaw

Before the first real run, check the script syntax with:

    python -m py_compile run.py

Then run:

    python run.py

Important
---------

A successfully downloaded recording is automatically deleted from ClickMeeting after its downloaded file size has been verified. Use a test recording for the first end-to-end run.

If you find this script useful, you can express your gratitude by supporting me with a coffee at https://www.buymeacoffee.com/slawciod82
