import requests
import json

#api psychologia euf90cd52e900a4245196929cc6037b2a34aea3d64
apikey = 'euf90cd52e900a4245196929cc6037b2a34aea3d64'
headers = { 'X-Api-Key' : apikey }
# response = requests.get('https://randomuser.me/api')

response = requests.get('https://api.clickmeeting.com/v1/conferences/active', headers=headers)
# sprawdza czy połączneie z api dzisiała
#print(response.status_code)
#print(response.json())

data = response.json()
for item in data:
    rec_response = requests.get(f'https://api.clickmeeting.com/v1/conferences/{item["id"]}/recordings', headers=headers)
    print(rec_response.json())
#    print(item["id"], item["name"])

#gender = response.json()['results'][0]['gender']
#print(gender)