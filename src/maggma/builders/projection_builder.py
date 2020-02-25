from maggma.builders import Builder
from pydash import get
from copy import deepcopy
from datetime import datetime
from maggma.utils import grouper
from typing import Union, Dict, List, Iterable, Any
from maggma.core import Store


class Projection_Builder(Builder):
    """
    This builder is used to create new documents that combines
    information from multiple input stores and stores these
    summary documents in the specified target store.

    Key values are used for matching such that multiple docs
    from the source_stores with the same key value will be
    combined into a single doc in the target store.

    Built in functionalities include user specification of which
    fields to project into the target store from each input store
    and limiting the builder to only consider certain key values.
    """

    def __init__(
        self,
        source_stores: List[Store],
        target_store: Store,
        fields_to_project: Union[List[Union[List, Dict]],None]=None,  #!!!check type hint validity
        query_by_key: List=None,
        **kwargs
    ):
        """
        Args:
            source_stores ([MongoStore]): list of stores. Fields from
                these input stores will be projected into target_store
            target_store (MongoStore): store where the aggregated
                output documents produced will be stored
            fields_to_project ([List,Dict]): If provided, the order of items in
                this list must correspond to source_stores. By default, all
                fields of source_stores are projected into target_store.
                List elements can be provided as 1) a list of strings specifying
                the fields to pull from each input store, or 2) a dictionary
                where the values specify the fields to pull from the input store and
                the keys specify what field that will be used in the target store.

                e.g. ["field1","field2"] would be equivalent to
                {"field1":"field1", "field2":"field2"}
                Or fields could be renamed in the target stores via
                {"newname1":"field1", "newname2":"field2"}

                If an empty list or dictionary is provided, all fields of that
                input store will be projected.

                Note fields_to_project is converted into the projection_mapping
                attribute of this builder. There are no checks for possible
                overwrite errors in output docs for the target_store.

            query_by_key (List): Provide a list of keys to limit this builder to a
                only consider a subset of docs with these key values. By default,
                every document from the input stores will be projected.
        """
        # check for user input errors
        if isinstance(source_stores, list) == False:
            raise TypeError("Input source_stores must be provided in a list")
        if isinstance(fields_to_project, list):
            if len(source_stores) != len(fields_to_project):
                raise ValueError(
                    "There must be an equal number of elements in source_stores and fields_to_project"
                )
        elif fields_to_project != None:
            raise TypeError(
                "Input fields_to_project must be a list. E.g. [['str1','str2'],{'A':'str1','B':str2'}]"
            )

        # interpret fields_to_project to create projection_mapping attribute
        if fields_to_project is None:
            projection_mapping = [None] * len(source_stores)
        else:
            projection_mapping = []
            for f in fields_to_project:
                if isinstance(f, (list)):
                    projection_mapping.append({i: i for i in f})
                elif isinstance(f, (dict)):
                    projection_mapping.append(f)
                else:
                    raise TypeError(
                        "Input fields_to_project elements must be a list or dict. E.g. [['str1','str2'],{'A':'str1','B':str2'}]"
                    )
            # ensure key is included in projection for get_items query
            for store, p in zip(source_stores, projection_mapping):
                if p != {}:
                    p.update({target_store.key: store.key})
        self.projection_mapping = projection_mapping

        # establish other attributes and initialization
        self.query_by_key = query_by_key or []
        self.target = target_store
        super().__init__(sources=source_stores, targets=target_store, **kwargs)
        self.ensure_indexes()

    def ensure_indexes(self):
        """
        Ensures key fields are indexed to improve querying efficiency
        """
        index_checks = [s.ensure_index(s.key) for s in self.sources]

        if not all(index_checks):
            self.logger.warning("Missing indices for key fields on stores.")

    def get_items(self) -> Iterable:
        """
        Gets items from source_stores for processing.
        Items are retrieved in chunks based on a subset of
        key values set by chunk_size but are unsorted.

        Returns:
            generator of items to process
        """
        self.logger.info("Starting {} get_items...".format(self.__class__.__name__))

        # get distinct key values
        if len(self.query_by_key) > 0:
            keys = self.query_by_key
        else:
            keys = set()
            for store in self.sources:
                store_keys = store.distinct(field=store.key)
                keys.update(store_keys)
                if None in store_keys:
                    self.logger.debug(
                        "None found as a key value for store {} with key {}".format(
                            store.collection_name, store.key
                        )
                    )
            keys = list(keys)
            self.logger.debug("{} distinct key values found".format(len(keys)))
            self.logger.debug("None found in key values? {}".format(None in keys))

        # for every key (in chunks), query from each store and
        # project fields specified by projection_mapping
        for chunked_keys in grouper(keys, self.chunk_size):
            chunked_keys = [k for k in chunked_keys if k is not None]
            #chunked_keys = list(chunked_keys) !!!
            self.logger.debug("Querying by chunked_keys: {}".format(chunked_keys))

            unsorted_items_to_process = []
            for store, projection in zip(self.sources, self.projection_mapping):

                # project all fields from store if corresponding element
                # in projection_mapping is an empty dict,
                # else only project the specified fields
                if projection == {}:  # all fields are projected
                    properties = None
                    self.logger.debug(
                        "For store {} getting all properties".format(
                            store.collection_name
                        )
                    )
                else:  # only specified fields are projected
                    properties = [v for v in projection.values()]
                    self.logger.debug(
                        "For {} store getting properties: {}".format(
                            store.collection_name, properties
                        )
                    )

                # get docs from store for given chunk of key values,
                # rename fields if specified by projection mapping,
                # and put in list of unsorted items to be processed
                docs = store.query(
                    criteria={store.key: {"$in": chunked_keys}}, properties=properties
                )
                for d in docs:
                    if properties is None:  # all fields are projected as is
                        item = deepcopy(d)
                    else:  # specified fields are renamed
                        item = {}
                        for k, v in projection.items():
                            item[k] = get(d, v)

                    # remove unneeded fields and add key value to each item
                    # key value stored under target_key is used for sorting
                    # items during the process_items step
                    for k in ["_id", store.last_updated_field]:
                        if k in item.keys():
                            del item[k]
                    item[self.target.key] = d[store.key]

                    unsorted_items_to_process.append(item)

                self.logger.debug(
                    "Example fields of one output item from {} store sent to process_items: {}".format(
                        store.collection_name, item.keys()
                    )
                )

            yield unsorted_items_to_process

    def process_item(self, items: Iterable) -> List:
        """
        Takes a chunk of items belonging to a subset of key values
        and groups them by key value. Combines items for each
        key value into one single doc for the target store.

        Arguments:
            items: items should all belong to a subset of
                key values but are not in any particular order
        Returns:
            items_for_target: a list of items where now each
                item corresponds to a single key value
        """
        self.logger.debug("Processing items: sorting by key values...")
        key = self.target.key
        items_sorted_by_key = {}
        for i in items:
            key_value = i[key]
            if key_value not in items_sorted_by_key.keys():
                items_sorted_by_key[key_value] = []
            items_sorted_by_key[key_value].append(i)

        items_for_target = []
        for k, i_sorted in items_sorted_by_key.items():
            self.logger.debug("Aggregating items for {}: {}".format(key, k))
            target_doc = {}
            for i in i_sorted:
                target_doc.update(i)
            # last modification is adding key value avoid overwriting
            target_doc[key] = k
            items_for_target.append(target_doc)
        # note target last_updated_field will be added during update_targets()

        return items_for_target

    def update_targets(self, items: List):
        """
        Adds a last_updated field to items and then adds
        them to the target store.

        Arguments:
            items: a list of items where each item contains
                all the information from the source_stores
                corresponding to a single key value
        """
        num_items = len(items)
        self.logger.debug("Updating target with {} items...".format(num_items))
        target = self.target

        target_insertion_time = datetime.utcnow()
        for item in items:
            item[target.last_updated_field] = target_insertion_time

        if num_items > 0:
            target.update(items)