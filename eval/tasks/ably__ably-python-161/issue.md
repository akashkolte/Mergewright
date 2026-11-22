Channel.publish sometimes returns None after exhausting retries
When trying to figure out the failure cases for `Channel.publish`, I found that 
if the method receives a series of 500 requests greater than the retry count, 
the method won't raise an error and will instead return `None`.

This means that to check for all errors with `Channel.publish` we need to write:

```python
channel = ably.channels.get(channel)
res = channel.publish(...)
if res is None:
    raise requests.ConnectionError
```

Instead of:

```python
channel = ably.channels.get(channel)
channel.publish(...)
```

I think the method should be updated to raise an exception after 
exhausting all retry attempts.

Something like:

```diff
diff --git a/ably/http/http.py b/ably/http/http.py
index 6e7bbbb..ea8067b 100644
--- a/ably/http/http.py
+++ b/ably/http/http.py
@@ -217,6 +217,7 @@ class Http:
                 except AblyException as e:
                     if not e.is_server_error:
                         raise e
+        raise AblyException(message="", status_code=0, code=0)
 
     def delete(self, url, headers=None, skip_auth=False, timeout=None):
         return self.make_request('DELETE', url, headers=headers,
```