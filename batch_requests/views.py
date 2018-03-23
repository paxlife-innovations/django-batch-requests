'''
@author: Rahul Tanwani

@summary: A module to perform batch request processing.
'''

import json
from datetime import datetime

from batch_requests.exceptions import BadBatchRequest
from batch_requests.settings import br_settings as _settings
from batch_requests.utils import get_wsgi_request_object
from django.http import Http404
from django.http.response import (HttpResponse, HttpResponseBadRequest,
                                  HttpResponseServerError)
from django.urls import resolve
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


def withDebugHeaders(view_handler):
    '''
        Decorator which wraps functions processing wsgi_requests and returning a dictionary,
        to which it adds a header item containing information about time taken and request url.
    '''
    def inner(wsgi_request):
        service_start_time = datetime.now()
        result = view_handler(wsgi_request)

        # Check if we need to send across the duration header.
        if not _settings.ADD_DURATION_HEADER:
            return result

        time_taken = (datetime.now() - service_start_time).microseconds / 1000

        result.setdefault('headers', {})
        result['headers'].update({
            'request_url': wsgi_request.path_info,
            _settings.DURATION_HEADER_NAME: time_taken,
        })
        return result
    return inner


@withDebugHeaders
def get_response(wsgi_request):
    '''
        Given a WSGI request, makes a call to a corresponding view
        function and returns the response.
    '''
    # Get the view / handler for this request
    try:
        view, args, kwargs = resolve(wsgi_request.path_info)
    except Http404 as error:
        return {'status_code': 404, 'reason_phrase': 'Page not found'}

    # Let the view do his task.
    kwargs.update({'request': wsgi_request})
    try:
        response = view(*args, **kwargs)
    except Exception as exc:
        response = HttpResponseServerError(content=str(exc))

    # Convert HTTP response into simple dict type.
    result = {
        'status_code': response.status_code,
        'reason_phrase': response.reason_phrase,
        'headers': dict(response._headers.values()),
    }

    # Make sure that the response has been rendered
    if hasattr(response, 'render') and callable(response.render):
        response.render()

    content = response.content
    if isinstance(content, bytes):
        content = content.decode('utf-8')

    try:
        content = json.loads(content)
    except json.JSONDecodeError:
        pass

    result['body'] = content
    return result


def get_wsgi_requests(request):
    '''
        For the given batch request, extract the individual requests and create
        WSGIRequest object for each.
    '''
    valid_http_methods = [
        'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'connect', 'trace'
    ]
    requests = json.loads(request.body).get('batch', [])

    if type(requests) not in (list, tuple):
        raise BadBatchRequest('The body of batch request should always be list!')

    # Max limit check.
    no_requests = len(requests)

    if no_requests > _settings.MAX_LIMIT:
        raise BadBatchRequest('You can batch maximum of %d requests.' % (_settings.MAX_LIMIT))

    # We could mutate the current request with the respective parameters, but mutation is ghost
    # in the dark, so lets avoid. Construct the new WSGI request object for each request.

    def construct_wsgi_from_data(data):
        '''
            Given the data in the format of url, method, body and headers, construct a new
            WSGIRequest object.
        '''
        url = data.get('url', None)
        method = data.get('method', None)

        if url is None or method is None:
            raise BadBatchRequest('Request definition should have url, method defined.')

        if method.lower() not in valid_http_methods:
            raise BadBatchRequest('Invalid request method.')

        body = None

        if method.lower() not in ['get', 'options']:
            body = data.get('body', '')

        headers = data.get('headers', {})
        return get_wsgi_request_object(request, method, url, headers, body)

    return [construct_wsgi_from_data(data) for data in requests]


def execute_requests(wsgi_requests):
    '''
        Execute the requests either sequentially or in parallel based on parallel
        execution setting.
    '''
    executor = _settings.executor
    return executor.execute(wsgi_requests, get_response)


@csrf_exempt
@require_http_methods(['POST'])
def handle_batch_requests(request, *args, **kwargs):
    '''
        A view function to handle the overall processing of batch requests.
    '''
    batch_start_time = datetime.now()
    try:
        # Get the Individual WSGI requests.
        wsgi_requests = get_wsgi_requests(request)
    except BadBatchRequest as brx:
        return HttpResponseBadRequest(content=str(brx))

    # Fire these WSGI requests, and collect the response for the same.
    response = execute_requests(wsgi_requests)

    # Evrything's done, return the response.
    resp = HttpResponse(content=json.dumps(response), content_type='application/json')

    if _settings.ADD_DURATION_HEADER:
        resp.__setitem__(
            _settings.DURATION_HEADER_NAME,
            str((datetime.now() - batch_start_time).microseconds / 1000)
        )
    return resp
