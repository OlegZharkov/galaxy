<%namespace name="ie" file="ie.mako" />

<%
import os
import shutil
import hashlib

# Sets ID and sets up a lot of other variables
ie_request.load_deploy_config()
ie_request.attr.docker_port = 8888
ie_request.attr.import_volume = False

if ie_request.attr.PASSWORD_AUTH:
    m = hashlib.sha1()
    m.update( ie_request.notebook_pw + ie_request.notebook_pw_salt )
    PASSWORD = 'sha1:%s:%s' % (ie_request.notebook_pw_salt, m.hexdigest())
else:
    PASSWORD = "none"

## Jupyter Notbook Specific
if hda.datatype.__class__.__name__ == "Ipynb":
    DATASET_HID = hda.hid
else:
    DATASET_HID = None

# Add all environment variables collected from Galaxy's IE infrastructure
ie_request.launch(env_override={
    'notebook_password': PASSWORD,
    'dataset_hid': DATASET_HID,
})

## General IE specific
# Access URLs for the notebook from within galaxy.
notebook_access_url = ie_request.url_template('${PROXY_URL}/ipython/notebooks/ipython_galaxy_notebook.ipynb')
notebook_login_url = ie_request.url_template('${PROXY_URL}/ipython/login?next=${PROXY_PREFIX}%2Fipython%2Ftree')

%>
<html>
<head>
${ ie.load_default_js() }
</head>
<body>

<script type="text/javascript">
${ ie.default_javascript_variables() }
var notebook_login_url = '${ notebook_login_url }';
var notebook_access_url = '${ notebook_access_url }';
${ ie.plugin_require_config() }

// Load notebook

requirejs(['interactive_environments', 'plugin/jupyter'], function(){
    load_notebook(ie_password, notebook_login_url, notebook_access_url);
});


// This is needed to keep the container alive. If the user leaves this site
// this function is not constantly pinging the container, the container will
// terminate itself.
var request_count = 0;
interval = setInterval(function(){
    $.ajax({
        url: notebook_access_url,
        xhrFields: {
            withCredentials: true
        },
        type: "GET",
        timeout: 500,
        success: function(){
            console.log("Connected to IE, returning");
        },
        error: function(jqxhr, status, error){
            request_count++;
            console.log("Request " + request_count);
            if(request_count > 30){
                clearInterval(interval);
                clear_main_area();
                toastr.error(
                    "Could not connect to IE, contact your administrator",
                    "Error",
                    {'closeButton': true, 'timeOut': 20000, 'tapToDismiss': false}
                );
            }
        }
    });
}, 30000);

</script>
<div id="main" width="100%" height="100%">
</div>
</body>
</html>
