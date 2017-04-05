# SMS + NLU Demo

## Install

These steps assume `Python 3.4+`, `PortAudio` and `Speex` are already installed. See the Mix.nlu Python sample app's README for more information.

You should also have a Twilio developer account, with a valid number that can send SMS.

Once you have all that, you can install your Python environment:

```shell
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

1. Copy the credentials file and contact list templates:

    ```shell
    cp contacts.sample.json contacts.json
    cp creds.sample.json creds.json
    ```

2. Create an empty Mix.nlu model, then upload `sms-model.trsx`;
3. Publish a version of the model, and create an app config (context tag of your choice) pointing to it;
4. Copy the Mix.nlu credentials for the app config, and update `creds.json`;
5. Get your Twilio developer credentials, and update `creds.json`;
6. Edit `contacts.json` with the contacts you wish to use.

The `LOG` variable in `nlu.py` can optionally be set to `False`,
in order to disable printing all WebSocket messages to the screen.

## Run

*Note: `user_id` should be set accordingly for each command*

### Data Upload

This command should be run first, with the actual list of contacts to be recognized.

```shell
python nlu.py --user_id=samueldg data_upload CONTACT contacts.json
```

*Note: There is a short delay before the data upload actually kicks in for the recognition. Doing a couple of NLU requests usually does the trick.*

### ASR + NLU

```shell
python nlu.py --user_id=samueldg audio
```

### Text + NLU

```shell
python nlu.py --user_id=samueldg text 'Say hi to myself'
```

### Data Wipe

```shell
python nlu.py --user_id=samueldg data_wipe
```
