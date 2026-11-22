upload REANA specification file to server by default
Originated in https://github.com/reanahub/reana-client/issues/620#issuecomment-1211638448 (point 1)

We do not upload the REANA specification files to the server. Having REANA specifications in the workspace will allow for easier debugs and more reproducibility (if we decide to export workflows to, for example, Zenodo).

The focus of this issue is to upload specifications by default in reana-client. This will help with transitioning to https://github.com/reanahub/reana-db/issues/162.

In addition, we can also add a validation step to reana-server to check if reana.yaml is uploaded.