"""Replay two canned replies through the existing design-choice workflow."""
import json
import os
from unittest.mock import patch

from jevkit import client, design_choice


def replay():
    def canned(confidence):
        reply = {'answers': {'preset': {
            'type': 'choice', 'choice': 'editorial', 'confidence': confidence,
            'probabilities': {'editorial': confidence, 'product': 1 - confidence},
        }}}
        return lambda *_: json.dumps(reply).encode()

    # Use the real request and response validation with a hardwired local transport.
    # Do not consult the environment's provider, endpoint, or actual keystore.
    with patch.dict(os.environ, {}, clear=True), \
            patch.object(client.keystore, 'provider', return_value='typesafe'), \
            patch.object(client.keystore, 'resolve', return_value='offline-fixture'), \
            patch.object(client, '_provider_transport', side_effect=AssertionError('offline only')):
        return {
            'source': 'canned responses', 'model_calls': 0,
            'accepted': design_choice.select('editorial', live=True, transport=canned(0.95)),
            'fallback': design_choice.select('product', live=True, transport=canned(0.55)),
        }


if __name__ == '__main__':
    print(json.dumps(replay(), indent=2))
