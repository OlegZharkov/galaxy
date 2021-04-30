"""
Manager and Serializer for histories.

Histories are containers for datasets or dataset collections
created (or copied) by users over the course of an analysis.
"""
import logging
from typing import (
    cast,
    List,
    Optional,
)

from pydantic import (
    BaseModel,
    Field,
)
from sqlalchemy import (
    and_,
    asc,
    desc
)

from galaxy import (
    exceptions as glx_exceptions,
    model
)
from galaxy.managers import (
    deletable,
    hdas,
    history_contents,
    sharable
)
from galaxy.schema.fields import EncodedDatabaseIdField
from galaxy.structured_app import MinimalManagerApp

log = logging.getLogger(__name__)


class HDABasicInfo(BaseModel):
    id: EncodedDatabaseIdField
    name: str


class ShareHistoryExtra(sharable.ShareWithExtra):
    can_change: List[HDABasicInfo] = Field(
        [],
        title="Can Change",
        description=(
            "A collection of datasets that are not accessible by one or more of the target users "
            "but, the user sharing the history, has permission to make them accessible for others."
        ),
    )
    cannot_change: List[HDABasicInfo] = Field(
        [],
        title="Cannot Change",
        description=(
            "A collection of datasets that are not accessible by one or more of the target users "
            "and, the user sharing the history, does not have permission to make them accessible for others."
        ),
    )
    num_accessible: int = Field(
        0,
        title="Accessible",
        description=(
            "The number of datasets in the history that are public or accessible by all the target users."
        ),
    )


class HistoryManager(sharable.SharableModelManager, deletable.PurgableManagerMixin):

    model_class = model.History
    foreign_key_name = 'history'
    user_share_model = model.HistoryUserShareAssociation

    tag_assoc = model.HistoryTagAssociation
    annotation_assoc = model.HistoryAnnotationAssociation
    rating_assoc = model.HistoryRatingAssociation

    # TODO: incorporate imp/exp (or alias to)

    def __init__(self, app: MinimalManagerApp, hda_manager: hdas.HDAManager, contents_manager: history_contents.HistoryContentsManager, contents_filters: history_contents.HistoryContentsFilters):
        super().__init__(app)
        self.hda_manager = hda_manager
        self.contents_manager = contents_manager
        self.contents_filters = contents_filters

    def copy(self, history, user, **kwargs):
        """
        Copy and return the given `history`.
        """
        return history.copy(target_user=user, **kwargs)

    # .... sharable
    # overriding to handle anonymous users' current histories in both cases
    def by_user(self, user, current_history=None, **kwargs):
        """
        Get all the histories for a given user (allowing anon users' theirs)
        ordered by update time.
        """
        # handle default and/or anonymous user (which still may not have a history yet)
        if self.user_manager.is_anonymous(user):
            return [current_history] if current_history else []
        return super().by_user(user, **kwargs)

    def is_owner(self, history, user, current_history=None, **kwargs):
        """
        True if the current user is the owner of the given history.
        """
        # anon users are only allowed to view their current history
        if self.user_manager.is_anonymous(user):
            if current_history and history == current_history:
                return True
            return False
        return super().is_owner(history, user)

    # TODO: possibly to sharable or base
    def most_recent(self, user, filters=None, current_history=None, **kwargs):
        """
        Return the most recently update history for the user.

        If user is anonymous, return the current history. If the user is anonymous
        and the current history is deleted, return None.
        """
        if self.user_manager.is_anonymous(user):
            return None if (not current_history or current_history.deleted) else current_history
        desc_update_time = desc(self.model_class.update_time)
        filters = self._munge_filters(filters, self.model_class.user_id == user.id)
        # TODO: normalize this return value
        return self.query(filters=filters, order_by=desc_update_time, limit=1, **kwargs).first()

    # .... purgable
    def purge(self, history, flush=True, **kwargs):
        """
        Purge this history and all HDAs, Collections, and Datasets inside this history.
        """
        self.hda_manager.dataset_manager.error_unless_dataset_purge_allowed()
        # First purge all the datasets
        for hda in history.datasets:
            if not hda.purged:
                self.hda_manager.purge(hda, flush=True)

        # Now mark the history as purged
        super().purge(history, flush=flush, **kwargs)

    # .... current
    # TODO: make something to bypass the anon user + current history permissions issue
    # def is_current_users_current_history( self, history, trans ):
    #     pass

    def get_current(self, trans):
        """
        Return the current history.
        """
        # TODO: trans
        return trans.get_history()

    def set_current(self, trans, history):
        """
        Set the current history.
        """
        # TODO: trans
        trans.set_history(history)
        return history

    def set_current_by_id(self, trans, history_id):
        """
        Set the current history by an id.
        """
        return self.set_current(trans, self.by_id(history_id))

    # order_by parsing - similar to FilterParser but not enough yet to warrant a class?
    def parse_order_by(self, order_by_string, default=None):
        """Return an ORM compatible order_by using the given string"""
        # TODO: generalize into class
        # TODO: general (enough) columns
        if order_by_string in ('create_time', 'create_time-dsc'):
            return desc(self.model_class.create_time)
        if order_by_string == 'create_time-asc':
            return asc(self.model_class.create_time)
        if order_by_string in ('update_time', 'update_time-dsc'):
            return desc(self.model_class.update_time)
        if order_by_string == 'update_time-asc':
            return asc(self.model_class.update_time)
        if order_by_string in ('name', 'name-asc'):
            return asc(self.model_class.name)
        if order_by_string == 'name-dsc':
            return desc(self.model_class.name)
        # TODO: history columns
        if order_by_string in ('size', 'size-dsc'):
            return desc(self.model_class.disk_size)
        if order_by_string == 'size-asc':
            return asc(self.model_class.disk_size)
        # TODO: add functional/non-orm orders (such as rating)
        if default:
            return self.parse_order_by(default)
        raise glx_exceptions.RequestParameterInvalidException('Unkown order_by', order_by=order_by_string,
            available=['create_time', 'update_time', 'name', 'size'])

    def non_ready_jobs(self, history):
        """Return the currently running job objects associated with this history.

        Where running is defined as new, waiting, queued, running, resubmitted,
        and upload.
        """
        # TODO: defer to jobModelManager (if there was one)
        # TODO: genericize the params to allow other filters
        jobs = (self.session().query(model.Job)
            .filter(model.Job.history == history)
            .filter(model.Job.state.in_(model.Job.non_ready_states)))
        return jobs

    def get_sharing_extra_information(
        self, trans, item, users, errors: List[str], option: Optional[sharable.SharingOptions] = None
    ) -> Optional[sharable.ShareWithExtra]:
        """Returns optional extra information about the datasets of the history that can be accessed by the users."""
        extra = ShareHistoryExtra()
        history = cast(model.History, item)
        if history.empty:
            errors.append("You cannot share an empty history.")
            return extra

        owner = trans.user
        owner_roles = owner.all_roles()
        can_change_dict = {}
        cannot_change_dict = {}
        total_datasets = 0
        for user in users:
            if self.is_history_shared_with(history, user):
                errors.append(f"History ({history.name}) already shared with user ({user.email})")
                continue

            user_roles = user.all_roles()
            # Only deal with datasets that have not been purged
            for hda in history.activatable_datasets:
                total_datasets += 1
                if trans.app.security_agent.can_access_dataset(user_roles, hda.dataset):
                    continue
                # The user with which we are sharing the history does not have access permission on the current dataset
                owner_can_manage_dataset = (
                    trans.app.security_agent.can_manage_dataset(owner_roles, hda.dataset)
                    and not hda.dataset.library_associations
                )
                if option and owner_can_manage_dataset:
                    if option == sharable.SharingOptions.make_accessible_to_shared:
                        trans.app.security_agent.privately_share_dataset(hda.dataset, users=[owner, user])
                    elif option == sharable.SharingOptions.make_public:
                        trans.app.security_agent.make_dataset_public(hda.dataset)
                elif not option or not owner_can_manage_dataset:
                    hda_id = trans.security.encode_id(hda.id)
                    hda_info = HDABasicInfo(id=hda_id, name=hda.name)
                    if owner_can_manage_dataset:
                        can_change_dict[hda_id] = hda_info
                    else:
                        cannot_change_dict[hda_id] = hda_info

        extra.can_change = list(can_change_dict.values())
        extra.cannot_change = list(cannot_change_dict.values())
        extra.num_accessible = total_datasets - len(extra.can_change) - len(extra.cannot_change)
        if not extra.num_accessible:
            errors.append("The history you are sharing do not contain any datasets that can be accessed by the users with which you are sharing.")

        extra.can_share = (
            not errors and extra.num_accessible == total_datasets
            or (not errors and option is not None and option == sharable.SharingOptions.no_changes)
        )
        return extra

    def is_history_shared_with(self, history, user) -> bool:
        return self.session().query(self.user_share_model).filter(
            and_(
                self.user_share_model.table.c.user_id == user.id,
                self.user_share_model.table.c.history_id == history.id,
            )
        ).count() > 0


class HistoryExportView:

    def __init__(self, app: MinimalManagerApp):
        self.app = app

    def get_exports(self, trans, history_id):
        history = self._history(trans, history_id)
        matching_exports = history.exports
        return [self.serialize(trans, history_id, e) for e in matching_exports]

    def serialize(self, trans, history_id, jeha):
        rval = jeha.to_dict()
        encoded_jeha_id = trans.security.encode_id(jeha.id)
        api_url = self.app.url_for("history_archive_download", id=history_id, jeha_id=encoded_jeha_id)
        # this URL is less likely to be blocked by a proxy and require an API key, so export
        # older-style controller version for use with within the GUI and such.
        external_url = self.app.url_for(controller='history', action="export_archive", id=history_id, qualified=True)
        external_permanent_url = self.app.url_for(controller='history', action="export_archive", id=history_id, jeha_id=encoded_jeha_id, qualified=True)
        rval["download_url"] = api_url
        rval["external_download_latest_url"] = external_url
        rval["external_download_permanent_url"] = external_permanent_url
        rval = trans.security.encode_all_ids(rval)
        return rval

    def get_ready_jeha(self, trans, history_id, jeha_id="latest"):
        history = self._history(trans, history_id)
        matching_exports = history.exports
        if jeha_id != "latest":
            decoded_jeha_id = trans.security.decode_id(jeha_id)
            matching_exports = [e for e in matching_exports if e.id == decoded_jeha_id]
        if len(matching_exports) == 0:
            raise glx_exceptions.ObjectNotFound("Failed to find target history export")

        jeha = matching_exports[0]
        if not jeha.ready:
            raise glx_exceptions.MessageException("Export not available or not yet ready.")

        return jeha

    def _history(self, trans, history_id):
        if history_id is not None:
            history = self.app.history_manager.get_accessible(trans.security.decode_id(history_id), trans.user, current_history=trans.history)
        else:
            history = trans.history
        return history


class HistorySerializer(sharable.SharableModelSerializer, deletable.PurgableSerializerMixin):
    """
    Interface/service object for serializing histories into dictionaries.
    """
    model_manager_class = HistoryManager
    SINGLE_CHAR_ABBR = 'h'

    def __init__(self, app: MinimalManagerApp, hda_manager: hdas.HDAManager, hda_serializer: hdas.HDASerializer, history_contents_serializer: history_contents.HistoryContentsSerializer):
        super().__init__(app)

        self.history_manager = self.manager
        self.hda_manager = hda_manager
        self.hda_serializer = hda_serializer
        self.history_contents_serializer = history_contents_serializer

        self.default_view = 'summary'
        self.add_view('summary', [
            'id',
            'model_class',
            'name',
            'deleted',
            'purged',
            # 'count'
            'url',
            # TODO: why these?
            'published',
            'annotation',
            'tags',
            'update_time',
        ])
        self.add_view('detailed', [
            'contents_url',
            'empty',
            'size',
            'user_id',
            'create_time',
            'update_time',
            'importable',
            'slug',
            'username_and_slug',
            'genome_build',
            # TODO: remove the next three - instead getting the same info from the 'hdas' list
            'state',
            'state_details',
            'state_ids',
            # 'community_rating',
            # 'user_rating',
        ], include_keys_from='summary')
        # in the Historys' case, each of these views includes the keys from the previous

        #: ..note: this is a custom view for newer (2016/3) UI and should be considered volatile
        self.add_view('dev-detailed', [
            'contents_url',
            'size',
            'user_id',
            'create_time',
            'update_time',
            'importable',
            'slug',
            'username_and_slug',
            'genome_build',
            # 'contents_states',
            'contents_active',
            'hid_counter',
        ], include_keys_from='summary')

        # beta web client fields, no summary/detailed/dev-detailed blah
        self.add_view('betawebclient', [
            'annotation',
            'contents_active',
            'contents_url',
            'create_time',
            'deleted',
            'empty',
            'genome_build',
            'hid_counter',
            'id',
            'importable',
            'name',
            'nice_size',
            'published',
            'purged',
            # 'shared',
            'size',
            'slug',
            'state',
            'tags',
            'update_time',
            'url',
            'username_and_slug',
            'user_id',
        ])

    # assumes: outgoing to json.dumps and sanitized
    def add_serializers(self):
        super().add_serializers()
        deletable.PurgableSerializerMixin.add_serializers(self)

        self.serializers.update({
            'model_class': lambda *a, **c: 'History',
            'size': lambda i, k, **c: int(i.disk_size),
            'nice_size': lambda i, k, **c: i.disk_nice_size,
            'state': self.serialize_history_state,

            'url': lambda i, k, **c: self.url_for('history', id=self.app.security.encode_id(i.id)),
            'contents_url': lambda i, k, **c: self.url_for('history_contents',
                                                           history_id=self.app.security.encode_id(i.id)),

            'empty': lambda i, k, **c: (len(i.datasets) + len(i.dataset_collections)) <= 0,
            'count': lambda i, k, **c: len(i.datasets),
            'hdas': lambda i, k, **c: [self.app.security.encode_id(hda.id) for hda in i.datasets],
            'state_details': self.serialize_state_counts,
            'state_ids': self.serialize_state_ids,
            'contents': self.serialize_contents,
            'non_ready_jobs': lambda i, k, **c: [self.app.security.encode_id(job.id) for job
                                                 in self.manager.non_ready_jobs(i)],

            'contents_states': self.serialize_contents_states,
            'contents_active': self.serialize_contents_active,
            #  TODO: Use base manager's serialize_id for user_id (and others)
            #  after refactoring hierarchy here?
            'user_id': lambda i, k, **c: self.app.security.encode_id(i.user_id) if i.user_id is not None else None
        })

    # remove this
    def serialize_state_ids(self, history, key, **context):
        """
        Return a dictionary keyed to possible dataset states and valued with lists
        containing the ids of each HDA in that state.
        """
        state_ids = {}
        for state in model.Dataset.states.values():
            state_ids[state] = []

        # TODO:?? collections and coll. states?
        for hda in history.datasets:
            # TODO: do not encode ids at this layer
            encoded_id = self.app.security.encode_id(hda.id)
            state_ids[hda.state].append(encoded_id)
        return state_ids

    # remove this
    def serialize_state_counts(self, history, key, exclude_deleted=True, exclude_hidden=False, **context):
        """
        Return a dictionary keyed to possible dataset states and valued with the number
        of datasets in this history that have those states.
        """
        # TODO: the default flags above may not make a lot of sense (T,T?)
        state_counts = {}
        for state in model.Dataset.states.values():
            state_counts[state] = 0

        # TODO:?? collections and coll. states?
        for hda in history.datasets:
            if exclude_deleted and hda.deleted:
                continue
            if exclude_hidden and not hda.visible:
                continue
            state_counts[hda.state] = state_counts[hda.state] + 1
        return state_counts

    # TODO: remove this (is state used/useful?)
    def serialize_history_state(self, history, key, **context):
        """
        Returns the history state based on the states of the HDAs it contains.
        """
        states = model.Dataset.states
        # (default to ERROR)
        state = states.ERROR
        # TODO: history_state and state_counts are classically calc'd at the same time
        #   so this is rel. ineff. - if we keep this...
        hda_state_counts = self.serialize_state_counts(history, 'counts', exclude_deleted=True, **context)
        num_hdas = sum(hda_state_counts.values())
        if num_hdas == 0:
            state = states.NEW

        else:
            if (hda_state_counts[states.RUNNING] > 0
                    or hda_state_counts[states.SETTING_METADATA] > 0
                    or hda_state_counts[states.UPLOAD] > 0):
                state = states.RUNNING
            # TODO: this method may be more useful if we *also* polled the histories jobs here too
            elif (hda_state_counts[states.QUEUED] > 0
                    or hda_state_counts[states.NEW] > 0):
                state = states.QUEUED
            elif (hda_state_counts[states.ERROR] > 0
                    or hda_state_counts[states.FAILED_METADATA] > 0):
                state = states.ERROR
            elif hda_state_counts[states.OK] == num_hdas:
                state = states.OK

        return state

    def serialize_contents(self, history, key, trans=None, user=None, **context):
        returned = []
        for content in self.manager.contents_manager._union_of_contents_query(history).all():
            serialized = self.history_contents_serializer.serialize_to_view(content,
                view='summary', trans=trans, user=user)
            returned.append(serialized)
        return returned

    def serialize_contents_states(self, history, key, trans=None, **context):
        """
        Return a dictionary containing the counts of all contents in each state
        keyed by the distinct states.

        Note: does not include deleted/hidden contents.
        """
        return self.manager.contents_manager.state_counts(history)

    def serialize_contents_active(self, history, key, **context):
        """
        Return a dictionary keyed with 'deleted', 'hidden', and 'active' with values
        for each representing the count of contents in each state.

        Note: counts for deleted and hidden overlap; In other words, a dataset that's
        both deleted and hidden will be added to both totals.
        """
        return self.manager.contents_manager.active_counts(history)


class HistoryDeserializer(sharable.SharableModelDeserializer, deletable.PurgableDeserializerMixin):
    """
    Interface/service object for validating and deserializing dictionaries into histories.
    """
    model_manager_class = HistoryManager

    def __init__(self, app: MinimalManagerApp):
        super().__init__(app)
        self.history_manager = self.manager

    def add_deserializers(self):
        super().add_deserializers()
        deletable.PurgableDeserializerMixin.add_deserializers(self)

        self.deserializers.update({
            'name': self.deserialize_basestring,
            'genome_build': self.deserialize_genome_build,
        })


class HistoryFilters(sharable.SharableModelFilters, deletable.PurgableFiltersMixin):
    model_class = model.History
    model_manager_class = HistoryManager

    def _add_parsers(self):
        super()._add_parsers()
        deletable.PurgableFiltersMixin._add_parsers(self)
        self.orm_filter_parsers.update({
            # history specific
            'name': {'op': ('eq', 'contains', 'like')},
            'genome_build': {'op': ('eq', 'contains', 'like')},
            'create_time': {'op': ('le', 'ge', 'gt', 'lt'), 'val': self.parse_date},
            'update_time': {'op': ('le', 'ge', 'gt', 'lt'), 'val': self.parse_date},
        })


class HistoriesService:
    """Common interface/service logic for interactions with histories in the context of the API.

    Provides the logic of the actions invoked by API controllers and uses type definitions
    and pydantic models to declare its parameters and return types.
    """

    def __init__(self, app: MinimalManagerApp, manager: HistoryManager, serializer: HistorySerializer):
        self.app = app
        self.manager = manager
        self.serializer = serializer
        self.shareable_service = sharable.ShareableService(self.manager, self.serializer)

    # TODO: add the rest of the API actions here and call them directly from the API controller
