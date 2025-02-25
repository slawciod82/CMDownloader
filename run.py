import requests
import os
from datetime import datetime
import app_cfg
# import json



apikeys = app_cfg.apikeys
account_names = app_cfg.account_names

for apikey, account_name in zip(apikeys, account_names):
    now: datetime = datetime.now()
    print(f'{now:%c}')
    headers = {'X-Api-Key': apikey}
    response = requests.get('https://api.clickmeeting.com/v1/conferences/active', headers=headers)
    # sprawdza czy połączneie z api dzisiała
    if response.status_code == 200:
        response_translation = " (ok)"
        print('status code=' + str(response.status_code) + response_translation + ' for account: ' + account_name)
        data = response.json()
        for room in data:
            print(f'room id: {room["id"]}')
            rec_response = requests.get(f'https://api.clickmeeting.com/v1/conferences/{room["id"]}/recordings',
                                        headers=headers)
            rec_data = rec_response.json()
            # print(rec_response.json())
            for rec in rec_data:
                rec_id = rec["id"]
                rec_url = rec["recording_url"]
                rec_started = rec["recorder_started"]
                rec_name = room["name"]
                rec_file_size = rec["recording_file_size"]

                sessions_response = requests.get(f'https://api.clickmeeting.com/v1/conferences/{room["id"]}/sessions',
                                                 headers=headers)
                sessions_data = sessions_response.json()

                print(f'sessions_data: {sessions_data}')

                for session in sessions_data:
                    print(f'session: {session}')
                    session_id = session["id"]
                    generate_pdf_url_response = requests.get(
                        f'https://api.clickmeeting.com/v1/conferences/{room["id"]}/sessions/{session_id}/generate-pdf/pl',
                        headers=headers)
                    generate_pdf_url_data = generate_pdf_url_response.json()
                    pdf_url = generate_pdf_url_data["url"]
                    response = requests.get(pdf_url)
                    pdf_to_save_as = f'raport - {room["name"]}.pdf'
                    with open(path, "wb") as f:
                        f.write(response.content)
                        path = f'{pdf_to_save_as}'
                        print(f'{path} saved')
                if int(rec_file_size) <= app_cfg.download_limit:
                    print(f'--- File size = {rec_file_size} is lower then download limit (download limit = {app_cfg.download_limit})! ---')
                    # rec_del_resp = requests.delete(f'https://api.clickmeeting.com/v1/conferences/{item["id"]}/recordings/{rec_id}', headers=headers)
                    # if rec_del_resp:
                    #    print('Record deleted')
                    # else:
                    #    print('--- ERROR: Unable to delete record! --- Check Your account ---')
                    print('--- TEST --- record size limit')
                else:
                    spec_char = ['/', '\\', '*', '\t']
                    for i in spec_char:
                        rec_name = rec_name.replace(i, "-")
                    if rec_id:
                        print(rec_id, rec_url, rec_started, rec_name, "original file size:" + rec_file_size)
                        response = requests.get(rec_url)
                        rec_started_split = rec_started.split(sep=" ")
                        rec_date = rec_started_split[0]
                        rec_time_split = rec_started_split[1].split(sep=":")
                        rec_time_h = int(rec_time_split[0]) + app_cfg.winter_time_mod
                        rec_time_m = rec_time_split[1]
                        rec_time_s = rec_time_split[2]
                        new_rec_time = str(str(rec_time_h).zfill(2) + '_' + rec_time_m + '_' + rec_time_s)
                        file_to_save_as = f'{rec_name} {rec_date} {new_rec_time}.mp4'
                        path_to_save_as = app_cfg.path_to_save
                        rec_dir = rec_date.replace("-", "_")
                        rec_dir_split = rec_dir.split(sep="_")
                        rec_year_dir = rec_dir_split[0]
                        rec_month_dir = rec_dir_split[1]
                        if not os.path.isdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir, rec_dir)):
                            if not os.path.isdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir)):
                                if not os.path.isdir(os.path.join(path_to_save_as, rec_year_dir)):
                                    os.mkdir(os.path.join(path_to_save_as, rec_year_dir))
                                    os.mkdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir))
                                    os.mkdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir, rec_dir))
                                else:
                                    os.mkdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir))
                                    os.mkdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir, rec_dir))
                            else:
                                os.mkdir(os.path.join(path_to_save_as, rec_year_dir, rec_month_dir, rec_dir))
                        path = os.path.join(path_to_save_as, rec_year_dir, rec_month_dir, rec_dir, file_to_save_as)
                        with open(path, "wb") as f:
                            f.write(response.content)
                            # path = f'{file_to_save_as}'
                            checkfile = os.path.exists(path)
                            file_stats = os.stat(path)
                            file_size = file_stats.st_size
                            print(file_size)
                            # delete conditions after record download
                            if int(rec_file_size) == int(file_size):
                                if checkfile:
                                    print('Download Completed')
                            #        rec_del_resp = requests.delete(f'https://api.clickmeeting.com/v1/conferences/{item["id"]}/recordings/{rec_id}', headers=headers)
                            #        if rec_del_resp:
                            #            print('Record Deleted')
                            #        else:
                                        # print('--- ERROR: Unable to delete record --- Check Your account --- ')
                                else:
                                    # print('--- ERROR: Download failed. wrong file name---')
                                    print("--- Testing... - delete skipped ---")
                            elif int(file_size) == 0:
                                print(f'--- ERROR: Record corrupted! File size = {file_size}! ---')
                            #    rec_del_resp = requests.delete(f'https://api.clickmeeting.com/v1/conferences/{item["id"]}/recordings/{rec_id}', headers=headers)
                            #    if rec_del_resp:
                            #        print('Corrupted record deleted')
                            #    else:
                                    # print('--- ERROR: Unable to delete record! --- Check Your account ---')
                                print(f'--- Testing... - ERROR: Record corrupted! File size = {file_size}! ---')
                                    # print('--- Testing... - Corrupted record deleted')
                                print('--- Testing... - delete skipped ---')
                            else:
                                print('--- ERROR: Download failed, file size mismatch --- Check Your account ---')
    else:
        response_translation = " (API not Responding !!!)"
        print('status code=' + str(response.status_code) + response_translation + ' for account: ' + account_name)
print('Job done!')
now: datetime = datetime.now()
print(f'{now:%c}')
# print('If you find this script useful, you can express your gratitude by supporting me with a coffee at https://www.buymeacoffee.com/slawciod82')
