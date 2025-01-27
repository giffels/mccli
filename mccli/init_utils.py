import requests
import requests_cache
import os.path
import liboidcagent as agent
from flaat import tokentools

from .motley_cue_client import local_username, get_supported_ops, is_valid_mc_url
from .ssh_wrapper import get_hostname
from .logging import logger

# some predefined oidc-gen commands for different issuers
oidc_gen_command_strings = {
    "aai.egi.eu/oidc": 'oidc-gen --pub --iss https://aai.egi.eu/oidc --scope "openid profile email offline_access eduperson_entitlement eduperson_scoped_affiliation eduperson_unique_id" egi',
    "wlcg.cloud.cnaf.infn.it": 'oidc-gen --pub --issuer https://wlcg.cloud.cnaf.infn.it --scope "openid profile offline_access eduperson_entitlement eduperson_scoped_affiliation wlcg.groups wlcg" wlcg',
    "login.helmholtz.de/oauth2": 'oidc-gen --pub --iss https://login.helmholtz.de/oauth2 --scope "openid profile email offline_access eduperson_entitlement eduperson_scoped_affiliation eduperson_unique_id" helmholtz',
    "accounts.google.com": 'oidc-gen --pub --iss https://accounts.google.com/ --flow device --scope max google'
}

def canonical_url(url):
    """Strip URL of protocol info and ending slashes
    """
    url = url.lower()
    if url.startswith("http://"):
        url = url[7:]
    if url.startswith("https://"):
        url = url[8:]
    if url.startswith("www."):
        url = url[4:]
    if url.endswith("/"):
        url = url[:-1]
    return url


def oidc_gen_command(iss):
    """Return a string containing the appropriate oidc-gen command
    for given issuer, including suggested scopes.
    """
    return oidc_gen_command_strings.get(canonical_url(iss), f"oidc-gen --iss {iss}")


def validate_token_length(func):
    """Decorator for init_token that checks if token length is < 1024

    The function takes token returned by init_token and raises an
    Exception if token too long and cannot be used for SSH authentication.
    """
    def wrapper(*args, **kwargs):
        at, str_get_at = func(*args, **kwargs)
        if kwargs.get("validate_length", True) and len(at) > 1024:
            raise Exception(f"Sorry, your token is too long ({len(at)} > 1024) and cannot be used for SSH authentication. Please ask your OP admin if they can release shorter tokens.")
        return at, str_get_at
    return wrapper


@validate_token_length
def init_token(token, oa_account, iss, mc_endpoint=None, verify=True, validate_length=True):
    """Retrieve an oidc token:

    * use token if set,
    * retrieve from the oidc-agent via given account if oa_account is set
    * retrieve from the oidc-agent via given issuer if iss is set
    * use iss from service, if only one iss is supported
    * fail

    return token and string representation of command to retrieve token
    """
    expired = False
    if token is not None:
        # check if token is expired
        info_in_token = tokentools.get_accesstoken_info(token)
        timeleft = tokentools.get_timeleft(info_in_token)
        if not timeleft:
            logger.warning("Could not get expiration date from provided token, it might not be a JWT. Using it anyway...")
            logger.debug(f"Access Token: {token}")
            return token, _str_init_token(token=token)
        elif timeleft > 0:
            logger.info(f"Token valid for {timeleft} more seconds, using provided token.")
            logger.debug(f"Access Token: {token}")
            return token, _str_init_token(token=token)
        else:
            expired = True
            logger.warning(f"Token is expired for {-timeleft} seconds. Looking for another source for Access Token...")
            logger.debug(f"Access Token: {token}")
    else:
        logger.info("No access token provided.")
    if oa_account is not None:
        try:
            logger.info(f"Using oidc-agent account: {oa_account}")
            return agent.get_access_token(
                oa_account, application_hint="mccli"
            ), _str_init_token(oa_account=oa_account)
        except Exception as e:
            logger.warning(f"Failed to get Access Token for oidc-agent account '{oa_account}': {e}.")
            logger.warning(f"Are you sure this account is loaded? Load it with:\n    oidc-add {oa_account}")
            logger.warning(f"Are you sure this account is configured? Create it with:\n    oidc-gen {oa_account}")
    else:
        logger.info("No oidc-agent account provided.")
    if iss is not None:
        try:
            logger.info(f"Using issuer: {iss}")
            if not iss.startswith("http"):
                iss = f"https://{iss}"
                logger.warning(f"The issuer URL you provided does not contain protocol information, assuming HTTPS: {iss}")
            return agent.get_access_token_by_issuer_url(
                iss, application_hint="mccli"
            ), _str_init_token(iss=iss)
        except Exception as e:
            logger.warning(f"Failed to get Access Token from oidc-agent for issuer '{iss}': {e}.")
            logger.warning(f"Are you sure the issuer URL is correct or that you have an account configured with oidc-agent for this issuer? Create it with:\n    {oidc_gen_command(iss)}")
    else:
        logger.info("No issuer URL provided.")
    if mc_endpoint is not None:
        logger.info(f"Trying to get list of supported AT issuers from {mc_endpoint}...")
        supported_ops = get_supported_ops(mc_endpoint, verify)
        if len(supported_ops) == 1:
            iss = supported_ops[0]
            try:
                logger.info(f"Using the only issuer supported on service to retrieve token from oidc-agent: {iss}")
                return agent.get_access_token_by_issuer_url(
                    iss, application_hint="mccli"
                ), _str_init_token(iss=iss)
            except Exception as e:
                logger.warning(f"Failed to get Access Token from oidc-agent for the only issuer supported on service '{iss}': {e}")
                logger.warning(f"If you don't have an oidc-agent account configured for this issuer, create it with:\n    {oidc_gen_command(iss)}")
        elif len(supported_ops) > 1:
            logger.warning("Multiple issuers supported on service, I don't know which one to use:")
            logger.warning("[" + '\n    '.join(['']+supported_ops) + "\n]")
    if expired:
        msg = "The provided Access Token is expired. Have you considered using 'oidc-agent' to always have valid tokens?\n" + \
              "    https://github.com/indigo-dc/oidc-agent\n" + \
              "Try 'mccli --help' for help on specifying the Access Token source."
    else:
        msg = "No Access Token found.\n" + \
              "Try 'mccli --help' for help on specifying the Access Token source."
    raise Exception(msg)


def _str_init_token(token=None, oa_account=None, iss=None):
    """String representation of command used to get Access Token:
    * full token if token is set
    * `oidc-token oa_account` if oidc-agent account is set
    * `oidc-token iss` if issuer is set
    *  ... (`oidc-token iss` is iss can be retrieved from service)
    """
    if token:
        return f"'{token}'"
    if oa_account:
        return f"`oidc-token {oa_account}`"
    elif iss:
        return f"`oidc-token {iss}`"
    raise Exception("No access token found")


def valid_mc_url(mc_endpoint, verify=True):
    """Checks if there is a motley_cue service
    running at provided url and returns the url
    Raises an exception otherwise.
    """
    if mc_endpoint.startswith("http"):
        valid_endpoint = is_valid_mc_url(mc_endpoint, verify)
        if valid_endpoint:
            return valid_endpoint
    else:
        for schema in ["http", "https"]:
            # they should be in this order, since https can raise SSL related exception
            logger.warning(f"No URL schema specified for mc-endpoint, trying {schema}")
            endpoint = f"{schema}://{mc_endpoint}"
            valid_endpoint = is_valid_mc_url(endpoint, verify)
            if valid_endpoint:
                return valid_endpoint
    msg = f"No motley_cue service found at '{mc_endpoint}'. "\
        "Please specify a valid motley_cue endpoint."
    raise Exception(msg)


def init_endpoint(ssh_args, verify=True):
    """Initialise motley_cue endpoint from ssh args.

    (HACKY) Try to get the ssh host from `ssh_args`
    by executing the ssh command with invalid `-b` option
    and parsing the output for the actual HOSTNAME.

    Then try to use default value for motley_cue endpoint: https://HOSTNAME
    If this is not reachable, issue warning and try: http://HOSTNAME:8080
    If also not reachable, exit and ask user to specify it using --mc-endpoint
    """
    logger.info("Trying to get ssh hostname from arguments.")
    ssh_host = get_hostname(ssh_args)
    if not ssh_host:  # raise error and ask user to specify endpoint
        msg = f"Could not resolve hostname."
        raise Exception(msg)
    logger.info(f"Got host \'{ssh_host}\', looking for motley_cue service on host.")

    # try https
    endpoint = f"https://{ssh_host}"
    valid_endpoint = is_valid_mc_url(endpoint, verify)
    if valid_endpoint:
        return valid_endpoint

    # try https on 8443
    endpoint = f"https://{ssh_host}:8443"
    valid_endpoint = is_valid_mc_url(endpoint, verify)
    if valid_endpoint:
        return valid_endpoint

    # try http on 8080 but issue warning
    endpoint = f"http://{ssh_host}:8080"
    valid_endpoint = is_valid_mc_url(endpoint)
    if valid_endpoint:
        logger.warning(f"using unencrypted motley_cue endpoint: {endpoint}")
        return valid_endpoint

    # raise error and ask user to specify endpoint
    msg = f"No motley_cue service found on host '{ssh_host}' "\
        "on port 443, 8443 or 8080. "\
        "Please specify motley_cue endpoint via --mc-endpoint."
    raise Exception(msg)


def init_user(mc_endpoint, token, verify=True):
    """Get remote username, will be deployed if it doesn't exist.
    """
    return local_username(mc_endpoint, token, verify=verify)


def augmented_scp_command(scp_command, token, oa_account, iss, verify=False):
    """Receives an ScpCommand object and adds the username for each
    host that is remote and has no user specified, by contacting
    the motley_cue service on that host.
    Returns new scp command as list, as well as the tokens that should be
    passed as passwords, in order. A third list contains the
    string representation of each command used to get each token.
    """
    scp_args = scp_command.opts
    tokens = []
    str_get_tokens = []
    for operand in scp_command.sources + [scp_command.target]:
        if operand.remote and operand.user is None:
            # this is definitely a motley_cue managed host
            logger.debug(f"Trying to get username from motley_cue service on {operand.host}.")
            mc_url = init_endpoint([operand.host], verify)
            at, str_get_at = init_token(token, oa_account, iss, mc_url, verify)
            username = init_user(mc_url, at, verify)
            scp_args += [operand.unsplit(username)]
            tokens += [at]
            str_get_tokens += [str_get_at]
        else:
            # add the operand as it was given
            scp_args += [operand.original_str]

    return scp_args, tokens, str_get_tokens


def init_cache():
    """Function to install caching for HTTP requests.

    Make sure to call this function after setting log level
    for full output.
    """
    cache_name = os.path.expanduser("~/.cache/mccli_cache")
    expire_after = 300  # 5 min
    include_get_headers = True
    allowable_methods = ('GET')
    urls_expire_after = {
        '*/user/get_status': 0,
        '*/user/deploy': 0,
    }

    try:
        requests_cache.install_cache(
            cache_name=cache_name,
            expire_after=expire_after,
            allowable_methods=allowable_methods,
            include_get_headers=include_get_headers,
            urls_expire_after=urls_expire_after
        )

        if requests_cache.is_installed():
            # requests_cache.remove_expired_responses()
            logger.debug(f"HTTP requests cache installed at {cache_name}.sqlite")
    except Exception as e:
        logger.debug(f"Could not install requests cache: {e}. Uninstalling cache...")
        requests_cache.uninstall_cache()
        logger.warning(f"Something went wrong when initialising cache, will not cache HTTP requests. Executing command might be slower.")
