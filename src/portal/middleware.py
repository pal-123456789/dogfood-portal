# src/portal/middleware.py -- six lines, and the only custom middleware in the project.
class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", "script-src 'self'")
        return response
