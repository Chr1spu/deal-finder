"""Feature pipeline: CLIP image embeddings (stage 3a), NLP attribute
extraction (stage 3b).

Layered the same way connectors/ is: pure compute lives in embeddings.py
(image in, vector out, no database), and the module that owns a Session and
decides what to embed lives in embed_listings.py.
"""
