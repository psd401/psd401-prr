"""In-memory stand-ins for the configured object store.

The installment tests hand these to ``prepare_installment`` and friends in
place of a boto3 client. ``get_calls`` and ``put_calls`` are always kept so a
test that cares about round trips can assert on them and one that does not
can ignore them.
"""

import io


class FakeBody(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class FakeObjectClient:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.put_calls = []
        self.get_calls = []

    def get_object(self, *, Bucket, Key):
        self.get_calls.append(Key)
        return {"Body": FakeBody(self.objects[Key])}

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        position = body.tell()
        payload = body.read()
        body.seek(position)
        self.objects[kwargs["Key"]] = payload
        self.put_calls.append({**kwargs, "Body": payload})
