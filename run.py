import requests
import os.path
#import json

api_psychologia = "euf90cd52e900a4245196929cc6037b2a34aea3d64"
api_ah200 = "eu0970f0e5d774d09189ec5030af8dfd82e6e942dd"
api_ah500 = "eu21245290f97d71d0bb9323d97f0eb79b50b9f4fc"
api_ah1550 = "eu2dbda1d93d854803d4d4872415b342a89ecd8919"
api_ah5100 = "euea89422400467515f5fc5815701100d1a39e58e4"
apikeys=[api_psychologia, api_ah200, api_ah500, api_ah1550, api_ah5100]

for apikey in apikeys:
    headers = { 'X-Api-Key' : apikey }
    response = requests.get('https://api.clickmeeting.com/v1/conferences/active', headers=headers)
# sprawdza czy połączneie z api dzisiała
    print(response.status_code)
# print(response.json())
    data = response.json()
    for item in data:
        # print(item["id"])
        rec_response = requests.get(f'https://api.clickmeeting.com/v1/conferences/{item["id"]}/recordings', headers=headers)
        rec_data = rec_response.json()
        # print(rec_response.json())
        for rec in rec_data:
            rec_id = rec["id"]
            rec_url = rec["recording_url"]
            rec_started = rec["recorder_started"]
            rec_name = item["name"]
            spec_char=['/','\\','*','\t']
            for i in spec_char:
                rec_name=rec_name.replace(i,"-")
            if rec_id:
                print(rec_id, rec_url, rec_started, rec_name)
                response = requests.get(rec_url)
                rec_started_split = rec_started.split(sep=" ")
                rec_date = rec_started_split[0]
                winter_time_mod = 1
                rec_time_split = rec_started_split[1].split(sep=":")
                rec_time_h = int(rec_time_split[0]) + winter_time_mod
                rec_time_m = rec_time_split[1]
                rec_time_s = rec_time_split[2]
                new_rec_time = str(str(rec_time_h) + '_' + rec_time_m + '_' + rec_time_s)
                file_to_save_as = f'{rec_name} {rec_date} {new_rec_time}.mp4'
                with open(file_to_save_as, "wb") as f:
                    f.write(response.content)
                    path = f'./{file_to_save_as}'
                    checkfile = os.path.exists(path)
                    if checkfile:
                        print('Download Completed')
                        rec_del_resp = requests.delete(f'https://api.clickmeeting.com/v1/conferences/{item["id"]}/recordings/{rec_id}', headers=headers)
                        print('Record Deleted')
                    else:
                        print("Download failed")