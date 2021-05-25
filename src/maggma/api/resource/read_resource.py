from typing import Any, Callable, Dict, List, Optional, Type
from inspect import signature

from fastapi import Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel

from maggma.api.models import Meta, Response
from maggma.api.query_operator import PaginationQuery, QueryOperator, SparseFieldsQuery, VersionQuery
from maggma.api.resource import Resource
from maggma.api.utils import STORE_PARAMS, attach_signature, merge_queries
from maggma.core import Store


class ReadOnlyResource(Resource):
    """
    Implements a REST Compatible Resource as a GET URL endpoint
    This class provides a number of convenience features
    including full pagination, field projection
    """

    def __init__(
        self,
        store: Store,
        model: Type[BaseModel],
        tags: Optional[List[str]] = None,
        query_operators: Optional[List[QueryOperator]] = None,
        key_fields: Optional[List[str]] = None,
        custom_endpoint_funcs: Optional[List[Callable]] = None,
        query: Optional[Dict] = None,
        enable_get_by_key: bool = True,
        enable_default_search: bool = True,
        include_in_schema: Optional[bool] = True,
    ):
        """
        Args:
            store: The Maggma Store to get data from
            model: The pydantic model this Resource represents
            tags: List of tags for the Endpoint
            query_operators: Operators for the query language
            key_fields: List of fields to always project. Default uses SparseFieldsQuery
                to allow user to define these on-the-fly.
            custom_endpoint_funcs: Custom endpoint preparation functions to be used
            enable_get_by_key: Enable default key route for endpoint.
            enable_default_search: Enable default endpoint search behavior.
            include_in_schema: Whether the endpoint should be shown in the documented schema.
        """
        self.store = store
        self.tags = tags or []
        self.query = query or {}
        self.key_fields = key_fields
        self.versioned = False
        self.custom_endpoint_funcs = custom_endpoint_funcs
        self.enable_get_by_key = enable_get_by_key
        self.enable_default_search = enable_default_search
        self.include_in_schema = include_in_schema
        self.response_model = Response[model]  # type: ignore

        self.query_operators = (
            query_operators
            if query_operators is not None
            else [
                PaginationQuery(),
                SparseFieldsQuery(model, default_fields=[self.store.key, self.store.last_updated_field],),
            ]
        )

        for qop_entry in self.query_operators:
            if isinstance(qop_entry, VersionQuery):
                self.versioned = True
                self.default_version = qop_entry.default_version

        super().__init__(model)

    def prepare_endpoint(self):
        """
        Internal method to prepare the endpoint by setting up default handlers
        for routes
        """

        if self.custom_endpoint_funcs is not None:
            for func in self.custom_endpoint_funcs:
                func(self)

        if self.enable_get_by_key:
            self.build_get_by_key()

        if self.enable_default_search:
            self.build_dynamic_model_search()

    def build_get_by_key(self):
        key_name = self.store.key
        model_name = self.model.__name__

        if self.key_fields is None:
            field_input = SparseFieldsQuery(self.model, [self.store.key, self.store.last_updated_field]).query
        else:

            def field_input():
                return {"properties": self.key_fields}

        if not self.versioned:

            async def get_by_key(
                key: str = Path(..., alias=key_name, title=f"The {key_name} of the {model_name} to get"),
                fields: STORE_PARAMS = Depends(field_input),
            ):
                f"""
                Get's a document by the primary key in the store

                Args:
                    {key_name}: the id of a single {model_name}

                Returns:
                    a single {model_name} document
                """
                self.store.connect()

                item = [
                    self.store.query_one(criteria={self.store.key: key, **self.query}, properties=fields["properties"],)
                ]

                if item == [None]:
                    raise HTTPException(
                        status_code=404, detail=f"Item with {self.store.key} = {key} not found",
                    )

                for operator in self.query_operators:
                    item = operator.post_process(item)

                response = {"data": item}
                return response

            self.router.get(
                f"/{{{key_name}}}/",
                response_description=f"Get an {model_name} by {key_name}",
                response_model=self.response_model,
                response_model_exclude_unset=True,
                tags=self.tags,
                include_in_schema=self.include_in_schema,
            )(get_by_key)

        else:

            async def get_by_key_versioned(
                key: str = Path(..., alias=key_name, title=f"The {key_name} of the {model_name} to get"),
                fields: STORE_PARAMS = Depends(field_input),
                version: str = Query(
                    self.default_version, description="Database version to query on formatted as YYYY_MM_DD",
                ),
            ):
                f"""
                Get's a document by the primary key in the store

                Args:
                    {key_name}: the id of a single {model_name}

                Returns:
                    a single {model_name} document
                """

                self.store = VersionQuery().versioned_store_setup(self.store, version)

                self.store.connect()

                item = [
                    self.store.query_one(criteria={self.store.key: key, **self.query}, properties=fields["properties"],)
                ]

                if item == [None]:
                    raise HTTPException(
                        status_code=404, detail=f"Item with {self.store.key} = {key} not found",
                    )

                for operator in self.query_operators:
                    item = operator.post_process(item)

                response = {"data": item}
                return response

            self.router.get(
                f"/{{{key_name}}}/",
                response_description=f"Get an {model_name} by {key_name}",
                response_model=self.response_model,
                response_model_exclude_unset=True,
                tags=self.tags,
                include_in_schema=self.include_in_schema,
            )(get_by_key_versioned)

    def build_dynamic_model_search(self):

        model_name = self.model.__name__

        async def search(**queries: Dict[str, STORE_PARAMS]) -> Dict:
            request: Request = queries.pop("request")  # type: ignore

            query_params = [
                entry for _, i in enumerate(self.query_operators) for entry in signature(i.query).parameters
            ]

            overlap = [key for key in request.query_params.keys() if key not in query_params]
            if any(overlap):
                raise HTTPException(
                    status_code=400,
                    detail="Request contains query parameters which cannot be used: {}".format(", ".join(overlap)),
                )

            query: Dict[Any, Any] = merge_queries(list(queries.values()))  # type: ignore
            query["criteria"].update(self.query)

            if self.versioned:
                self.store = VersionQuery().versioned_store_setup(self.store, query["criteria"].get("version", None))
                query["criteria"].pop("version")

            self.store.connect()

            count = self.store.count(query["criteria"])
            data = list(self.store.query(**query))

            for operator in self.query_operators:
                data = operator.post_process(data)

            meta = Meta(total=count)
            response = {"data": data, "meta": meta.dict()}
            return response

        self.router.get(
            "/",
            tags=self.tags,
            summary=f"Get {model_name} documents",
            response_model=self.response_model,
            response_description=f"Search for a {model_name}",
            response_model_exclude_unset=True,
        )(attach_query_ops(search, self.query_operators))


def attach_query_ops(
    function: Callable[[List[STORE_PARAMS]], Dict], query_ops: List[QueryOperator]
) -> Callable[[List[STORE_PARAMS]], Dict]:
    """
    Attach query operators to API compliant function
    The function has to take a list of STORE_PARAMs as the only argument

    Args:
        function: the function to decorate
    """
    attach_signature(
        function,
        annotations={**{f"dep{i}": STORE_PARAMS for i, _ in enumerate(query_ops)}, "request": Request},
        defaults={f"dep{i}": Depends(dep.query) for i, dep in enumerate(query_ops)},
    )
    return function
