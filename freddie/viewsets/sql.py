from typing import (
    Any,
    AsyncIterable,
    Callable,
    Dict,
    Iterable,
    Iterator,
    Optional,
    Tuple,
    Type,
    Union,
)

from starlette.requests import Request

from ..db.models import (
    CharField,
    DBField,
    FieldsMap,
    ForeignKeyField,
    ManyToManyField,
    Model,
    PropsDependenciesMap,
)
from ..db.queries import (
    JOIN,
    Expression,
    Prefetch,
    Query,
    get_related,
    prefetch_related,
    set_related,
)
from ..exceptions import NotFound, ServerError, Unprocessable, db_errors_handler
from ..helpers import init_sql_logger, is_iterable
from ..schemas import Schema
from .dependencies import FilterBy, Paginator, ResponseFieldsDict
from .generics import (
    FIELDS_PARAM_NAME,
    CreateViewset,
    DestroyViewset,
    GenericViewSet,
    ListViewset,
    RetrieveViewset,
    UpdateViewset,
)

FK_FIELD_POSTFIX = '_id'
M2M_FIELD_POSTFIX = '_ids'
ModelPK = Any
ModelData = Dict[str, Any]
ModelRelations = Iterable[Tuple[ManyToManyField, set]]


class GenericModelViewSet(GenericViewSet):
    model: Type[Model] = Model
    pk_field: DBField
    secondary_lookup_field: Optional[DBField] = None
    _model_fields: FieldsMap
    _model_props_dependencies: PropsDependenciesMap

    def __init__(
        self, *args: Any, model: Type[Model] = None, sql_debug: bool = False, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        self.model = self.validate_model(model or self.model)
        self.pk_field = self.model.pk_field()
        self._model_fields = self.get_validated_model_fields()
        self._model_props_dependencies = self.model.map_props_dependencies()
        if self.validate_response:
            self.schema.__config__.orm_mode = True
        if sql_debug:
            init_sql_logger()

    def validate_model(self, model: Type[Model]) -> Type[Model]:
        assert isinstance(model, type), f'Schema for {type(self).__name__} must be a class'
        assert issubclass(
            model, Model
        ), f'Schema for {type(self).__name__} must be subclassed from {Model.__name__}'
        assert model.db() is not None, f'{self.model.__name__} model database not set'
        assert model.manager is not None, f'{model.__name__} model database manager not set'
        if self.secondary_lookup_field is not None:
            assert len(self._pk_type_choices) > 1, 'Secondary lookup field type must be set'
            assert self.secondary_lookup_field.unique, 'Non unique secondary lookup field'
        return model

    def get_validated_model_fields(self) -> FieldsMap:
        fields = {}
        writable_schema_fields = self.schema.get_writable_fields()
        for field_name, db_field in self.model.fields().items():
            if isinstance(db_field, CharField) and field_name in writable_schema_fields:
                schema_max_length = self.schema.get_field_max_length(field_name)
                if not schema_max_length or schema_max_length > db_field.max_length:
                    raise AssertionError(
                        f'{self.schema.__name__}.{field_name} '
                        f'maxlength not set or greater than DB field maxlength'
                    )
            fields[field_name] = db_field
        return fields

    def lookup_expr(self, pk: Any) -> Expression:
        if self.secondary_lookup_field is not None and type(pk) != self._pk_type_choices[0]:
            return self.secondary_lookup_field == pk
        return self.pk_field == pk

    def get_query_filters(self, request_filter_params: FilterBy = None) -> Iterator[Expression]:
        if request_filter_params is not None:
            for field_name, value in request_filter_params.items():
                model_field = self._model_fields.get(field_name)
                if model_field:
                    yield model_field == value

    def apply_query_filters(self, query: Query, request_filter_params: FilterBy = None) -> Query:
        filter_expressions = tuple(
            self.get_query_filters(request_filter_params=request_filter_params)
        )
        return query.where(*filter_expressions) if filter_expressions else query

    def get_base_query(self, fields: ResponseFieldsDict) -> Query:
        selected = set()
        joined = set()
        model_fields = {field_name: self._model_fields.get(field_name) for field_name in fields}
        for field_name, db_field in model_fields.items():
            # Foreign key ID
            if db_field is None and field_name.endswith(FK_FIELD_POSTFIX):
                selected.add(self._model_fields.get(field_name[: -len(FK_FIELD_POSTFIX)]))

            # Model property/getter method decorated with @depends_on
            elif db_field is None:
                dependencies: Iterable[DBField] = self._model_props_dependencies.get(field_name, [])
                for required_field in dependencies:
                    selected.add(required_field)
                fk_dependencies: Iterator[ForeignKeyField] = filter(
                    lambda f: isinstance(f, ForeignKeyField), dependencies
                )
                for fk in fk_dependencies:
                    joined.add(fk.rel_model)
                    selected.add(fk.rel_model)

            # Add related models
            # TODO: select only necessary related model fields
            elif isinstance(db_field, ForeignKeyField):
                joined.add(db_field.rel_model)
                selected.add(db_field.rel_model)

            # Just normal DB column to select
            else:
                selected.add(db_field)

        query = self.model.select(*(selected or (self.pk_field,)))
        for joined_model in joined:
            query = query.join_from(self.model, joined_model, JOIN.LEFT_OUTER)
        return query

    def build_prefetch_config(self, fields: ResponseFieldsDict) -> Iterator[Prefetch]:
        for field_name in fields:
            attr_name = field_name
            ids_only = field_name.endswith(M2M_FIELD_POSTFIX)
            if ids_only:
                field_name = field_name[: -len(M2M_FIELD_POSTFIX)]
            field = self.model.manytomany.get(field_name)
            if field:
                yield Prefetch(
                    field=field, attr_name=attr_name, ids_only=ids_only,
                )

    async def get_object_or_404(self, pk: Any, fields: ResponseFieldsDict = None) -> Model:
        query = self.get_base_query(fields or {})
        query = self.apply_query_filters(query).where(self.lookup_expr(pk))
        try:
            obj = await self.model.manager.get(query)
        except self.model.DoesNotExist:
            raise NotFound(f'{self._component_name.title()} not found')
        related_config = list(self.build_prefetch_config(fields or {}))
        if related_config:
            related = await get_related(obj.pk, related_config)
            for attr_name, items in related.items():
                setattr(obj, attr_name, items)
        return obj

    def serialize_request_body_for_db(
        self, body: Schema, on_create: bool = False
    ) -> Tuple[ModelData, ModelRelations]:
        data = {}
        related = []
        excluded_keys = self.schema.get_read_only_fields() | {self.pk_field.name}
        serialized = body.dict(
            exclude=excluded_keys, exclude_unset=not on_create, exclude_none=True, by_alias=True,
        )
        for key, value in serialized.items():
            if key not in self._model_fields:
                # Handle one-to-many relations
                if key.endswith(FK_FIELD_POSTFIX):
                    field_name = key[: -len(FK_FIELD_POSTFIX)]
                    if field_name in self._model_fields:
                        data[field_name] = value
                # Handle many-to-many relations
                elif key.endswith(M2M_FIELD_POSTFIX) and is_iterable(value):
                    field_name = key[: -len(M2M_FIELD_POSTFIX)]
                    field = self.model.manytomany.get(field_name)
                    if field is not None:
                        related.append((field, set(value)))
                continue
            data[key] = value
        return data, related


class ModelRetrieveViewset(GenericModelViewSet, RetrieveViewset):
    async def retrieve(self, pk: Any, *, request: Request, **params: Any) -> Model:
        fields = params.get(FIELDS_PARAM_NAME) or self.schema.get_default_response_fields_config()
        return await self.get_object_or_404(pk, fields=fields)


class ModelListViewset(GenericModelViewSet, ListViewset):
    async def list(
        self, *, request: Request, **params: Any,
    ) -> Union[Iterable[Model], AsyncIterable[Model]]:
        fields = params.get(FIELDS_PARAM_NAME) or self.schema.get_default_response_fields_config()
        query = self.get_base_query(fields)
        filter_by: Optional[FilterBy] = params.get(FilterBy.PARAM_NAME)
        if filter_by:
            query = self.apply_query_filters(query, request_filter_params=filter_by)
        paginator: Optional[Paginator] = params.get(Paginator.PARAM_NAME)
        if paginator:
            query = self.paginate_query(query, paginator)
        objects = await self.model.manager.execute(query)
        prefetched_config = list(self.build_prefetch_config(fields))
        if prefetched_config:
            return prefetch_related(objects, prefetched_config)
        return (obj for obj in objects)

    def paginate_query(self, query: Query, paginator: Paginator) -> Query:
        if paginator.limit:
            query = query.limit(paginator.limit)
        if paginator.offset:
            query = query.offset(paginator.offset)
        return query


class ModelCreateViewset(GenericModelViewSet, CreateViewset):
    async def perform_api_action(self, handler: Callable, *args: Any, **kwargs: Any) -> Any:
        if handler == self.create:
            pk = await super().perform_api_action(handler, *args, **kwargs)
            return await self.get_object_or_404(pk, fields=self._response_fields_full_config)
        return await super().perform_api_action(handler, *args, **kwargs)

    async def create(self, body: Schema, *, request: Request, **params: Any) -> ModelPK:
        data, related = self.serialize_request_body_for_db(body, on_create=True)
        if not data:
            raise Unprocessable('Empty request body')
        with db_errors_handler():
            pk = await self.perform_create(data, request=request, **params)
        if not pk:
            raise ServerError(f'{self._component_name.title()} not created')  # pragma: no cover
        with db_errors_handler():
            for field, ids in related:
                if not ids:
                    continue
                await set_related(pk, field, ids)
        return pk

    async def perform_create(self, data: ModelData, **params: Any) -> Any:
        query = self.model.insert(**data)
        return await self.model.manager.execute(query)


class ModelUpdateViewset(GenericModelViewSet, UpdateViewset):
    async def perform_api_action(self, handler: Callable, *args: Any, **kwargs: Any) -> Any:
        if handler == self.update:
            pk = await super().perform_api_action(handler, *args, **kwargs)
            return await self.get_object_or_404(pk, fields=self._response_fields_full_config)
        return await super().perform_api_action(handler, *args, **kwargs)

    async def update(
        self, pk: ModelPK, body: Schema, *, request: Request, **params: Any
    ) -> ModelPK:
        data, related = self.serialize_request_body_for_db(body, on_create=False)
        if data:
            with db_errors_handler():
                updated = await self.perform_update(pk, data, request=request, **params)
            if not updated:
                raise ServerError(f'{self._component_name.title()} not updated')  # pragma: no cover
        with db_errors_handler():
            for field, ids in related:
                await set_related(pk, field, ids)
        return pk

    async def perform_update(self, pk: ModelPK, data: ModelData, **params: Any) -> Any:
        query = self.model.update(**data)
        query = self.apply_query_filters(query).where(self.lookup_expr(pk))
        return await self.model.manager.execute(query)


class ModelDestroyViewset(GenericModelViewSet, DestroyViewset):
    async def destroy(self, pk: ModelPK, *, request: Request, **params: Any) -> None:
        deleted = await self.perform_destroy(pk, request=request, **params)
        if not deleted:
            raise ServerError(f'{self._component_name.title()} not deleted')  # pragma: no cover

    async def perform_destroy(self, pk: ModelPK, **params: Any) -> Any:
        query = self.model.delete()
        query = self.apply_query_filters(query).where(self.lookup_expr(pk))
        return await self.model.manager.execute(query)


class ReadOnlyModelViewSet(ModelListViewset, ModelRetrieveViewset):
    ...


class ListCreateModelViewSet(ModelListViewset, ModelCreateViewset):
    ...


class RetrieveUpdateModelViewSet(ModelRetrieveViewset, ModelUpdateViewset):
    ...


class RetrieveUpdateModelDestroyViewSet(
    ModelRetrieveViewset, ModelUpdateViewset, ModelDestroyViewset
):
    ...


class ModelViewSet(
    ModelListViewset,
    ModelRetrieveViewset,
    ModelCreateViewset,
    ModelUpdateViewset,
    ModelDestroyViewset,
):
    ...