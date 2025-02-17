"""CAS authentication backend"""

#from urllib import urlencode, urlopen
#from urlparse import urljoin
from six.moves.urllib_parse import urlencode, urljoin
from six.moves.urllib.request import urlopen

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django_cas.models import User, Tgt, PgtIOU
from django_cas.utils import cas_response_callbacks

__all__ = ['CASBackend']

def _verify_cas1(ticket, service):
    """Verifies CAS 1.0 authentication ticket.

    Returns username on success and None on failure.
    """

    params = {'ticket': ticket, 'service': service}
    url = (urljoin(settings.CAS_SERVER_URL, 'validate') + '?' +
           urlencode(params))
    page = urlopen(url)
    try:
        verified = page.readline().strip()
        if verified == 'yes':
            return page.readline().strip(), None
        else:
            return None, None
    finally:
        page.close()


def _verify_cas2(ticket, service):
    """Verifies CAS 2.0+ XML-based authentication ticket.

    Returns username on success and None on failure.
    """

    try:
        from xml.etree import ElementTree
    except ImportError:
        from elementtree import ElementTree

    if settings.CAS_PROXY_CALLBACK:
        params = {'ticket': ticket, 'service': service, 'pgtUrl': settings.CAS_PROXY_CALLBACK}
    else:
        params = {'ticket': ticket, 'service': service}

    url = (urljoin(settings.CAS_SERVER_URL, 'proxyValidate') + '?' +
           urlencode(params))

    page = urlopen(url)
    try:
        response = page.read()
        tree = ElementTree.fromstring(response)

        #Useful for debugging
        #from xml.dom.minidom import parseString
        #from xml.etree import ElementTree
        #txt = ElementTree.tostring(tree)
        #print parseString(txt).toprettyxml()
        
        if tree[0].tag.endswith('authenticationSuccess'):
            if settings.CAS_RESPONSE_CALLBACKS:
                cas_response_callbacks(tree)
            return tree[0][0].text, None
        else:
            return None, None
    finally:
        page.close()


def verify_proxy_ticket(ticket, service):
    """Verifies CAS 2.0+ XML-based proxy ticket.

    Returns username on success and None on failure.
    """

    try:
        from xml.etree import ElementTree
    except ImportError:
        from elementtree import ElementTree

    params = {'ticket': ticket, 'service': service}

    url = (urljoin(settings.CAS_SERVER_URL, 'proxyValidate') + '?' +
           urlencode(params))

    page = urlopen(url)

    try:
        response = page.read()
        tree = ElementTree.fromstring(response)
        if tree[0].tag.endswith('authenticationSuccess'):
            username = tree[0][0].text
            proxies = []
            if len(tree[0]) > 1:
                for element in tree[0][1]:
                    proxies.append(element.text)
            return {"username": username, "proxies": proxies}, None
        else:
            return None, None
    finally:
        page.close()


def get_saml_assertion(ticket):
   return """<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><SOAP-ENV:Header/><SOAP-ENV:Body><samlp:Request xmlns:samlp="urn:oasis:names:tc:SAML:1.0:protocol"  MajorVersion="1" MinorVersion="1" RequestID="_192.168.16.51.1024506224022" IssueInstant="2002-06-19T17:03:44.022Z"><samlp:AssertionArtifact>""" + ticket + """</samlp:AssertionArtifact></samlp:Request></SOAP-ENV:Body></SOAP-ENV:Envelope>"""

SAML_1_0_NS = 'urn:oasis:names:tc:SAML:1.0:'
SAML_1_0_PROTOCOL_NS = '{' + SAML_1_0_NS + 'protocol' + '}'
SAML_1_0_ASSERTION_NS = '{' + SAML_1_0_NS + 'assertion' + '}'

def _verify_cas2_saml(ticket, service):
    import urllib2
    """Verifies CAS 3.0+ XML-based authentication ticket and returns extended attributes.

    @date: 2011-11-30
    @author: Carlos Gonzalez Vila <carlewis@gmail.com>

    Returns username and attributes on success and None,None on failure.
    """

    try:
        from xml.etree import ElementTree
    except ImportError:
        from elementtree import ElementTree

    # We do the SAML validation
    headers = {'soapaction': 'http://www.oasis-open.org/committees/security',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'accept': 'text/xml',
        'connection': 'keep-alive',
        'content-type': 'text/xml'}
    params = {'TARGET': service}
    url = urllib2.Request(urljoin(settings.CAS_SERVER_URL, 'samlValidate') + '?' + urlencode(params), '', headers)
    data = get_saml_assertion(ticket)
    url.add_data(get_saml_assertion(ticket))

    page = urlopen(url)

    try:
        user = None
        attributes = {}
        response = page.read()
        tree = ElementTree.fromstring(response)
        # Find the authentication status
        success = tree.find('.//' + SAML_1_0_PROTOCOL_NS + 'StatusCode')
        if success is not None and success.attrib['Value'] == 'samlp:Success':
            # User is validated
            attrs = tree.findall('.//' + SAML_1_0_ASSERTION_NS + 'Attribute')
            for at in attrs:
                if 'uid' in at.attrib.values():
                    user = at.find(SAML_1_0_ASSERTION_NS + 'AttributeValue').text
                    attributes['uid'] = user
                values = at.findall(SAML_1_0_ASSERTION_NS + 'AttributeValue')
                if len(values) > 1:
                    values_array = []
                    for v in values:
                        values_array.append(v.text)
                    attributes[at.attrib['AttributeName']] = values_array
                else:
                   attributes[at.attrib['AttributeName']] = values[0].text
        return user, attributes
    finally:
        page.close()


_PROTOCOLS = {'1': _verify_cas1, '2': _verify_cas2, 'CAS_2_SAML_1_0': _verify_cas2_saml}

if settings.CAS_VERSION not in _PROTOCOLS:
    raise ValueError('Unsupported CAS_VERSION %r' % settings.CAS_VERSION)

_verify = _PROTOCOLS[settings.CAS_VERSION]


class CASBackend(object):
    """CAS authentication backend"""

    def authenticate(self, ticket, service, request):
        """Verifies CAS ticket and gets or creates User object"""

        username, attributes = _verify(ticket, service)
        if attributes:
            request.session['attributes'] = attributes
        if not username:
            return None
        
        last_name = attributes.get('surname', '')
        first_name = attributes.get('givenname', '')
        email = attributes.get('mail', '')
        
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            # user will have an "unusable" password
            user = User.objects.create_user(username, email, '')
            user.first_name = first_name
            user.last_name = last_name
            user.save()
        return user

    def get_user(self, user_id):
        """Retrieve the user's entry in the User model if it exists"""

        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
