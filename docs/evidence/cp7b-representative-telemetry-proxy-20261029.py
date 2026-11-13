"""Test-only upstream delay proxy; never installed in a production service."""
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response


@asynccontextmanager
async def lifespan(app):
    app.state.client = httpx.AsyncClient(timeout=15, trust_env=False,
        limits=httpx.Limits(max_connections=80,max_keepalive_connections=40))
    yield
    await app.state.client.aclose()


app=FastAPI(lifespan=lifespan)


@app.get('/health')
async def health():
    return {'status':'ok'}


@app.api_route('/{service}/{path:path}',methods=['GET','POST'])
async def proxy(service: str,path: str,request: Request):
    if service not in ('patient','emergency','admin'):
        return Response(status_code=404)
    await asyncio.sleep(.3)
    headers={key:value for key,value in request.headers.items() if key.lower() not in ('host','connection','content-length')}
    result=await app.state.client.request(request.method,f'http://{service}:8000/{path}',
        params=request.query_params,headers=headers,content=await request.body())
    return Response(content=result.content,status_code=result.status_code,
        media_type=result.headers.get('content-type','application/json'))
