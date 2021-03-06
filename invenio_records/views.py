# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2012, 2013, 2014, 2015 CERN.
#
# Invenio is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""WebSearch Flask Blueprint."""

from __future__ import unicode_literals

import cStringIO
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, \
    render_template, request, send_file, url_for

from flask_breadcrumbs import default_breadcrumb_root

from flask_login import current_user

from flask_menu import register_menu

from invenio.base.decorators import wash_arguments
from invenio.base.globals import cfg
from invenio.base.i18n import _
from invenio.base.signals import pre_template_render
from invenio.ext.template.context_processor import \
    register_template_context_processor
from invenio.utils import apache

from .signals import record_viewed
from .utils import visible_collection_tabs

blueprint = Blueprint('record', __name__, url_prefix="/record",  # FIXME
                      static_url_path='/record', template_folder='templates',
                      static_folder='static')

default_breadcrumb_root(blueprint, '.')


def request_record(f):
    """Perform standard operation to check record availability for user."""
    @wraps(f)
    def decorated(recid, *args, **kwargs):
        from invenio.modules.collections.models import Collection
        from invenio.legacy.search_engine import \
            guess_primary_collection_of_a_record

        from .api import get_record
        from .access import check_user_can_view_record
        from .models import Record as Bibrec
        # ensure recid to be integer
        recid = int(recid)
        g.bibrec = Bibrec.query.get(recid)

        g.record = record = get_record(recid)
        if record is None:
            abort(404)

        g.collection = collection = Collection.query.filter(
            Collection.name == guess_primary_collection_of_a_record(recid)).\
            one()

        (auth_code, auth_msg) = check_user_can_view_record(current_user, recid)

        # only superadmins can use verbose parameter for obtaining debug
        # information
        if not current_user.is_super_admin and 'verbose' in kwargs:
            kwargs['verbose'] = 0

        if auth_code:
            flash(auth_msg, 'error')
            abort(apache.HTTP_UNAUTHORIZED)

        # from invenio.legacy.search_engine import record_exists, \
        #     get_merged_recid
        # check if the current record has been deleted
        # and has been merged, case in which the deleted record
        # will be redirect to the new one
        # record_status = record_exists(recid)
        # merged_recid = get_merged_recid(recid)
        # if record_status == -1 and merged_recid:
        #     return redirect(url_for('record.metadata', recid=merged_recid))
        # elif record_status == -1:
        #     abort(apache.HTTP_GONE)  # The record is gone!

        title = record.get(cfg.get('RECORDS_BREADCRUMB_TITLE_KEY'), '')
        tabs = []

        def _format_record(record, of='hd', user_info=current_user, *args,
                           **kwargs):
            from invenio.modules.formatter import format_record
            return format_record(record, of, user_info=user_info, *args,
                                 **kwargs)

        @register_template_context_processor
        def record_context():
            # from invenio.modules.comments.api import get_mini_reviews
            return dict(recid=recid,
                        record=record,
                        tabs=tabs,
                        title=title,
                        get_mini_reviews=lambda *args, **kwargs: '',
                        # FIXME get_mini_reviews,
                        collection=collection,
                        format_record=_format_record
                        )

        pre_template_render.send(
            "%s.%s" % (blueprint.name, f.__name__),
            recid=recid,
        )
        return f(recid, *args, **kwargs)
    return decorated


@blueprint.route('/<int:recid>/metadata', methods=['GET', 'POST'])
@blueprint.route('/<int:recid>/', methods=['GET', 'POST'])
@blueprint.route('/<int:recid>', methods=['GET', 'POST'])
@blueprint.route('/<int:recid>/export/<of>', methods=['GET', 'POST'])
@wash_arguments({'of': (unicode, 'hd'), 'ot': (unicode, None)})
@request_record
@register_menu(blueprint, 'record.metadata', _('Information'), order=1,
               endpoint_arguments_constructor=lambda:
               dict(recid=request.view_args.get('recid')),
               visible_when=visible_collection_tabs('metadata'))
def metadata(recid, of='hd', ot=None):
    """Display formated record metadata."""
    # from invenio.legacy.bibrank.downloads_similarity import \
    #     register_page_view_event
    from invenio.modules.formatter import get_output_format_content_type
    # register_page_view_event(recid, current_user.get_id(),
    #                          str(request.remote_addr))
    if get_output_format_content_type(of) != 'text/html':
        from invenio.modules.search.views.search import \
            response_formated_records
        return response_formated_records([recid], g.collection, of, qid=None)

    # Send the signal 'document viewed'
    record_viewed.send(
        current_app._get_current_object(),
        recid=recid,
        id_user=current_user.get_id(),
        request=request)

    return render_template('records/metadata.html', of=of, ot=ot)


@blueprint.route('/<int:recid>/files', methods=['GET', 'POST'])
@request_record
@register_menu(blueprint, 'record.files', _('Files'), order=8,
               endpoint_arguments_constructor=lambda:
               dict(recid=request.view_args.get('recid')),
               visible_when=visible_collection_tabs('files'))
def files(recid):
    """Return overview of attached files."""
    def get_files():
        from invenio.legacy.bibdocfile.api import BibRecDocs
        for bibdoc in BibRecDocs(recid).list_bibdocs():
            for file in bibdoc.list_all_files():
                yield file.get_url()

    return render_template('records/files.html', files=list(get_files()))


@blueprint.route('/<int:recid>/files/<path:filename>', methods=['GET'])
@request_record
def file(recid, filename):
    """Serve attached documents."""
    from invenio.modules.documents import api
    duuids = [uuid for (k, uuid) in g.record.get('_documents', [])
              if k == filename]
    error = 404
    for duuid in duuids:
        document = api.Document.get_document(duuid)
        if not document.is_authorized(current_user):
            current_app.logger.info(
                "Unauthorized access to /{recid}/files/{filename} "
                "({document}) by {current_user}".format(
                    recid=recid, filename=filename, document=document,
                    current_user=current_user))
            error = 401
            continue

        # TODO add logging of downloads

        if document.get('linked', False):
            if document.get('uri').startswith('http://') or \
                    document.get('uri').startswith('https://'):
                return redirect(document.get('uri'))

            # FIXME create better streaming support

            file_ = cStringIO.StringIO(document.open('rb').read())
            file_.seek(0)
            return send_file(file_, mimetype='application/octet-stream',
                             attachment_filename=filename)
        return send_file(document['uri'])

    from invenio.modules.documents.utils import _get_legacy_bibdocs
    for fullpath, permission in _get_legacy_bibdocs(recid, filename=filename):
        if not permission:
            error = 401
            continue
        return send_file(fullpath)

    abort(error)


@blueprint.route('/', methods=['GET', 'POST'])
def no_recid():
    """Redirect to homepage."""
    return redirect("/")
