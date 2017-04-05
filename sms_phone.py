import json

import twilio
import twilio.rest


class SmsPhone:

    def __init__(self, account_sid, auth_token, from_number):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.client = twilio.rest.TwilioRestClient(account_sid, auth_token)

    @classmethod
    def from_json(cls, config_path):
        """Creates an Phone object from a given JSON configurations
        The JSON must include a `twilio_account_sid`, a `twilio_auth_token`,
        and a `twilio_from_number`.
        """

        with open(config_path, 'r') as f:
            credentials = json.load(f)

        account_sid = credentials['twilio_account_sid']
        auth_token = credentials['twilio_auth_token']
        from_number = credentials['twilio_from_number']
        return cls(account_sid, auth_token, from_number)

    def send_sms(self, body, to):
        """Sends an SMS to a recipient, containing the given body.
        The SMS will be sent from the account initially configured.
        """
        try:
            self.client.messages.create(
                body=body,
                to=to,
                from_=self.from_number,
            )
        except twilio.TwilioRestException as e:
            print(e)
