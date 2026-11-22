304 wrongly documented as following ErrorSchema
Automatic doc says 304 comes with a JSON payload. This is not true.

From https://httpstatuses.com/304:

> A 304 response cannot contain a message-body; it is always terminated by the first empty line after the header fields.

Flask/werkzeug respect that and 304 responses have an empty body.

Autodoc should be fixed.

We should clarify first which codes are concerned. There could be more than just 304 concerned.

Also, when `NotModified` is raised, we go through the error handler. This is useless since the payload is trashed. I don't think we should worry about that.