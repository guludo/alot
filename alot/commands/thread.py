# Copyright (C) 2011-2012  Patrick Totzke <patricktotzke@gmail.com>
# Copyright © 2018 Dylan Baker
# This file is released under the GNU GPL, version 3 or a later revision.
# For further details see the COPYING file
import argparse
import logging
import mailcap
import os
import subprocess
import tempfile
import email
import email.policy
from email.utils import getaddresses, parseaddr
from email.message import Message

import urwid
from io import BytesIO

from . import Command, registerCommand
from .globals import ExternalCommand
from .globals import FlushCommand
from .globals import ComposeCommand
from .globals import MoveCommand
from .globals import CommandCanceled
from .common import RetagPromptCommand
from .envelope import SendCommand
from ..completion.contacts import ContactsCompleter
from ..completion.path import PathCompleter
from ..db.utils import decode_header
from ..db.utils import formataddr
from ..db.utils import get_body_part
from ..db.utils import extract_headers
from ..db.utils import clear_my_address
from ..db.utils import ensure_unique_address
from ..db.envelope import Envelope
from ..db.attachment import Attachment
from ..db.errors import DatabaseROError
from ..settings.const import settings
from ..helper import parse_mailcap_nametemplate
from ..helper import split_commandstring
from ..utils import argparse as cargparse
from ..utils import ansi
from ..widgets.globals import AttachmentWidget

MODE = 'thread'


def determine_sender(mail, action='reply'):
    """
    Inspect a given mail to reply/forward/bounce and find the most appropriate
    account to act from and construct a suitable From-Header to use.

    :param mail: the email to inspect
    :type mail: `email.message.Message`
    :param action: intended use case: one of "reply", "forward" or "bounce"
    :type action: str
    """
    assert action in ['reply', 'forward', 'bounce']

    # get accounts
    my_accounts = settings.get_accounts()
    assert my_accounts, 'no accounts set!'

    # extract list of addresses to check for my address
    # X-Envelope-To and Envelope-To are used to store the recipient address
    # if not included in other fields
    # Process the headers in order of importance: if a mail was sent with
    # account X, with account Y in e.g. CC or delivered-to, make sure that
    # account X is the one selected and not account Y.
    candidate_headers = settings.get("reply_account_header_priority")
    for candidate_header in candidate_headers:
        candidate_addresses = getaddresses(mail.get_all(candidate_header, []))

        logging.debug('candidate addresses: %s', candidate_addresses)
        # pick the most important account that has an address in candidates
        # and use that account's realname and the address found here
        for account in my_accounts:
            for seen_name, seen_address in candidate_addresses:
                if account.matches_address(seen_address):
                    if settings.get(action + '_force_realname'):
                        realname = account.realname
                    else:
                        realname = seen_name
                    if settings.get(action + '_force_address'):
                        address = str(account.address)
                    else:
                        address = seen_address

                    logging.debug('using realname: "%s"', realname)
                    logging.debug('using address: %s', address)

                    from_value = formataddr((realname, address))
                    return from_value, account

    # revert to default account if nothing found
    account = my_accounts[0]
    realname = account.realname
    address = account.address
    logging.debug('using realname: "%s"', realname)
    logging.debug('using address: %s', address)

    from_value = formataddr((realname, str(address)))
    return from_value, account


@registerCommand(MODE, 'reply', arguments=[
    (['--all'], {'action': 'store_true', 'help': 'reply to all'}),
    (['--list'], {'action': cargparse.BooleanAction, 'default': None,
                  'dest': 'listreply', 'help': 'reply to list'}),
    (['--spawn'], {'action': cargparse.BooleanAction, 'default': None,
                   'help': 'open editor in new window'})])
class ReplyCommand(Command):

    """reply to message"""
    repeatable = True

    def __init__(self, message=None, all=False, listreply=None, spawn=None,
                 **kwargs):
        """
        :param message: message to reply to (defaults to selected message)
        :type message: `alot.db.message.Message`
        :param all: group reply; copies recipients from Bcc/Cc/To to the reply
        :type all: bool
        :param listreply: reply to list; autodetect if unset and enabled in
                          config
        :type listreply: bool
        :param spawn: force spawning of editor in a new terminal
        :type spawn: bool
        """
        self.message = message
        self.groupreply = all
        self.listreply = listreply
        self.force_spawn = spawn
        Command.__init__(self, **kwargs)

    async def apply(self, ui):
        # get message to reply to if not given in constructor
        if not self.message:
            self.message = ui.current_buffer.get_selected_message()
        mail = self.message.get_email()

        # set body text
        name, address = parseaddr(mail['From'])
        timestamp = self.message.get_date()
        qf = settings.get_hook('reply_prefix')
        if qf:
            quotestring = qf(name, address, timestamp,
                             message=mail, ui=ui, dbm=ui.dbman)
        else:
            quotestring = 'Quoting %s (%s)\n' % (name or address, timestamp)
        mailcontent = quotestring
        quotehook = settings.get_hook('text_quote')
        body_text = ansi.strip_ansi_escapes(self.message.get_body_text())
        if quotehook:
            mailcontent += quotehook(body_text)
        else:
            quote_prefix = settings.get('quote_prefix')
            for line in body_text.splitlines():
                mailcontent += quote_prefix + line + '\n'

        envelope = Envelope(bodytext=mailcontent, replied=self.message)

        # copy subject
        subject = decode_header(mail.get('Subject', ''))
        reply_subject_hook = settings.get_hook('reply_subject')
        if reply_subject_hook:
            subject = reply_subject_hook(subject)
        else:
            rsp = settings.get('reply_subject_prefix')
            if not subject.lower().startswith(('re:', rsp.lower())):
                subject = rsp + subject
        envelope.add('Subject', subject)

        # Auto-detect ML
        auto_replyto_mailinglist = settings.get('auto_replyto_mailinglist')
        if mail['List-Id'] and self.listreply is None:
            # mail['List-Id'] is need to enable reply-to-list
            self.listreply = auto_replyto_mailinglist
        elif mail['List-Id'] and self.listreply is True:
            self.listreply = True
        elif self.listreply is False:
            # In this case we only need the sender
            self.listreply = False

        # set From-header and sending account
        try:
            from_header, account = determine_sender(mail, 'reply')
        except AssertionError as e:
            ui.notify(str(e), priority='error')
            return
        envelope.add('From', from_header)
        envelope.account = account

        # set To
        sender = mail['Reply-To'] or mail['From']
        sender_address = parseaddr(sender)[1]
        cc = []

        # check if reply is to self sent message
        if account.matches_address(sender_address):
            recipients = mail.get_all('To', [])
            emsg = 'Replying to own message, set recipients to: %s' \
                % recipients
            logging.debug(emsg)
        else:
            recipients = [sender]

        if self.groupreply:
            # make sure that our own address is not included
            # if the message was self-sent, then our address is not included
            MFT = mail.get_all('Mail-Followup-To', [])
            followupto = clear_my_address(account, MFT)
            if followupto and settings.get('honor_followup_to'):
                logging.debug('honor followup to: %s', ', '.join(followupto))
                recipients = followupto
                # since Mail-Followup-To was set, ignore the Cc header
            else:
                if sender != mail['From']:
                    recipients.append(mail['From'])

                # append To addresses if not replying to self sent message
                if not account.matches_address(sender_address):
                    cleared = clear_my_address(account,
                                               mail.get_all('To', []))
                    recipients.extend(cleared)

                # copy cc for group-replies
                if 'Cc' in mail:
                    cc = clear_my_address(account, mail.get_all('Cc', []))
                    envelope.add('Cc', decode_header(', '.join(cc)))

        to = ', '.join(ensure_unique_address(recipients))
        logging.debug('reply to: %s', to)

        if self.listreply:
            # To choose the target of the reply --list
            # Reply-To is standart reply target RFC 2822:, RFC 1036: 2.2.1
            # X-BeenThere is needed by sourceforge ML also winehq
            # X-Mailing-List is also standart and is used by git-send-mail
            to = mail['Reply-To'] or mail['X-BeenThere'] or mail['X-Mailing-List']

            # Some mail server (gmail) will not resend you own mail, so you
            # have to deal with the one in sent
            if to is None:
                to = mail['To']
            logging.debug('mail list reply to: %s', to)
            # Cleaning the 'To' in this case
            if envelope.get('To') is not None:
                envelope.__delitem__('To')

        # Finally setup the 'To' header
        envelope.add('To', decode_header(to))

        # if any of the recipients is a mailinglist that we are subscribed to,
        # set Mail-Followup-To header so that duplicates are avoided
        if settings.get('followup_to'):
            # to and cc are already cleared of our own address
            allrecipients = [to] + cc
            lists = settings.get('mailinglists')
            # check if any recipient address matches a known mailing list
            if any(addr in lists for n, addr in getaddresses(allrecipients)):
                followupto = ', '.join(allrecipients)
                logging.debug('mail followup to: %s', followupto)
                envelope.add('Mail-Followup-To', decode_header(followupto))

        # set In-Reply-To header
        envelope.add('In-Reply-To', '<%s>' % self.message.get_message_id())

        # set References header
        old_references = mail.get('References', '')
        if old_references:
            old_references = old_references.split()
            references = old_references[-8:]
            if len(old_references) > 8:
                references = old_references[:1] + references
            references.append('<%s>' % self.message.get_message_id())
            envelope.add('References', ' '.join(references))
        else:
            envelope.add('References', '<%s>' % self.message.get_message_id())

        # continue to compose
        encrypt = mail.get_content_subtype() == 'encrypted'
        await ui.apply_command(ComposeCommand(envelope=envelope,
                                              spawn=self.force_spawn,
                                              encrypt=encrypt))


@registerCommand(MODE, 'forward', arguments=[
    (['--attach'], {'action': 'store_true', 'help': 'attach original mail'}),
    (['--spawn'], {'action': cargparse.BooleanAction, 'default': None,
                   'help': 'open editor in new window'})])
class ForwardCommand(Command):

    """forward message"""
    repeatable = True

    def __init__(self, message=None, attach=True, spawn=None, **kwargs):
        """
        :param message: message to forward (defaults to selected message)
        :type message: `alot.db.message.Message`
        :param attach: attach original mail instead of inline quoting its body
        :type attach: bool
        :param spawn: force spawning of editor in a new terminal
        :type spawn: bool
        """
        self.message = message
        self.inline = not attach
        self.force_spawn = spawn
        Command.__init__(self, **kwargs)

    async def apply(self, ui):
        # get message to forward if not given in constructor
        if not self.message:
            self.message = ui.current_buffer.get_selected_message()
        mail = self.message.get_email()

        envelope = Envelope(passed=self.message)

        if self.inline:  # inline mode
            # set body text
            name, address = self.message.get_author()
            timestamp = self.message.get_date()
            qf = settings.get_hook('forward_prefix')
            if qf:
                quote = qf(name, address, timestamp,
                           message=mail, ui=ui, dbm=ui.dbman)
            else:
                quote = 'Forwarded message from %s (%s):\n' % (
                    name or address, timestamp)
            mailcontent = quote
            quotehook = settings.get_hook('text_quote')
            if quotehook:
                mailcontent += quotehook(self.message.get_body_text())
            else:
                quote_prefix = settings.get('quote_prefix')
                for line in self.message.get_body_text().splitlines():
                    mailcontent += quote_prefix + line + '\n'

            envelope.body_txt = mailcontent

            for a in self.message.get_attachments():
                envelope.attach(a)

        else:  # attach original mode
            # attach original msg
            original_mail = Message()
            original_mail.set_type('message/rfc822')
            original_mail['Content-Disposition'] = 'attachment'
            original_mail.set_payload(mail.as_string(policy=email.policy.SMTP))
            envelope.attach(Attachment(original_mail))

        # copy subject
        subject = decode_header(mail.get('Subject', ''))
        subject = 'Fwd: ' + subject
        forward_subject_hook = settings.get_hook('forward_subject')
        if forward_subject_hook:
            subject = forward_subject_hook(subject)
        else:
            fsp = settings.get('forward_subject_prefix')
            if not subject.startswith(('Fwd:', fsp)):
                subject = fsp + subject
        envelope.add('Subject', subject)

        # Set forwarding reference headers
        envelope.add('References', '<%s>' % self.message.get_message_id())
        envelope.add('X-Forwarded-Message-Id', '<%s>' % self.message.get_message_id())

        # set From-header and sending account
        try:
            from_header, account = determine_sender(mail, 'reply')
        except AssertionError as e:
            ui.notify(str(e), priority='error')
            return
        envelope.add('From', from_header)
        envelope.account = account

        # continue to compose
        await ui.apply_command(ComposeCommand(envelope=envelope,
                                              spawn=self.force_spawn))


@registerCommand(MODE, 'bounce')
class BounceMailCommand(Command):

    """directly re-send selected message"""
    repeatable = True

    def __init__(self, message=None, **kwargs):
        """
        :param message: message to bounce (defaults to selected message)
        :type message: `alot.db.message.Message`
        """
        self.message = message
        Command.__init__(self, **kwargs)

    async def apply(self, ui):
        # get mail to bounce
        if not self.message:
            self.message = ui.current_buffer.get_selected_message()
        mail = self.message.get_email()

        # look if this makes sense: do we have any accounts set up?
        my_accounts = settings.get_accounts()
        if not my_accounts:
            ui.notify('no accounts set', priority='error')
            return

        # remove "Resent-*" headers if already present
        del mail['Resent-From']
        del mail['Resent-To']
        del mail['Resent-Cc']
        del mail['Resent-Date']
        del mail['Resent-Message-ID']

        # set Resent-From-header and sending account
        try:
            resent_from_header, account = determine_sender(mail, 'bounce')
        except AssertionError as e:
            ui.notify(str(e), priority='error')
            return
        mail['Resent-From'] = resent_from_header

        # set Reset-To
        allbooks = not settings.get('complete_matching_abook_only')
        logging.debug('allbooks: %s', allbooks)
        if account is not None:
            abooks = settings.get_addressbooks(order=[account],
                                               append_remaining=allbooks)
            logging.debug(abooks)
            completer = ContactsCompleter(abooks)
        else:
            completer = None
        to = await ui.prompt('To', completer=completer,
                             history=ui.recipienthistory)
        if to is None:
            raise CommandCanceled()

        mail['Resent-To'] = to.strip(' \t\n,')

        logging.debug("bouncing mail")
        logging.debug(mail.__class__)

        await ui.apply_command(SendCommand(mail=mail))


@registerCommand(MODE, 'editnew', arguments=[
    (['--spawn'], {'action': cargparse.BooleanAction, 'default': None,
                   'help': 'open editor in new window'})])
class EditNewCommand(Command):

    """edit message in as new"""
    def __init__(self, message=None, spawn=None, **kwargs):
        """
        :param message: message to reply to (defaults to selected message)
        :type message: `alot.db.message.Message`
        :param spawn: force spawning of editor in a new terminal
        :type spawn: bool
        """
        self.message = message
        self.force_spawn = spawn
        Command.__init__(self, **kwargs)

    async def apply(self, ui):
        if not self.message:
            self.message = ui.current_buffer.get_selected_message()
        mail = self.message.get_email()
        # copy most tags to the envelope
        tags = set(self.message.get_tags())
        tags.difference_update({'inbox', 'sent', 'draft', 'killed', 'replied',
                                'signed', 'encrypted', 'unread', 'attachment'})
        tags = list(tags)

        draft = None
        if 'draft' in self.message.get_tags():
            draft = self.message

        # set body text
        mailcontent = self.message.get_body_text(render=False)
        envelope = Envelope(bodytext=mailcontent, tags=tags,
                            previous_draft=draft)

        # copy selected headers
        to_copy = ['Subject', 'From', 'To', 'Cc', 'Bcc', 'In-Reply-To',
                   'References']
        for key in to_copy:
            value = decode_header(mail.get(key, ''))
            if value:
                envelope.add(key, value)

        # copy attachments
        for b in self.message.get_attachments():
            envelope.attach(b)

        await ui.apply_command(ComposeCommand(envelope=envelope,
                                              spawn=self.force_spawn,
                                              omit_signature=True))


@registerCommand(
    MODE, 'fold', help='fold message(s)', forced={'visible': False},
    arguments=[(['query'], {'help': 'query used to filter messages to affect',
                            'nargs': '*'})])
@registerCommand(
    MODE, 'unfold', help='unfold message(s)', forced={'visible': True},
    arguments=[(['query'], {'help': 'query used to filter messages to affect',
                            'nargs': '*'})])
@registerCommand(
    MODE, 'togglesource', help='display message source',
    forced={'raw': 'toggle'},
    arguments=[(['query'], {'help': 'query used to filter messages to affect',
                            'nargs': '*'})])
@registerCommand(
    MODE, 'toggleheaders', help='display all headers',
    forced={'all_headers': 'toggle'},
    arguments=[(['query'], {'help': 'query used to filter messages to affect',
                            'nargs': '*'})])
@registerCommand(
    MODE, 'indent', help='change message/reply indentation',
    arguments=[(['indent'], {'action': cargparse.ValidatedStoreAction,
                             'validator': cargparse.is_int_or_pm})])
@registerCommand(
    MODE, 'togglemimetree', help='disply mime tree of the message',
    forced={'mimetree': 'toggle'},
    arguments=[(['query'], {'help': 'query used to filter messages to affect',
                            'nargs': '*'})])
@registerCommand(
    MODE, 'togglemimepart', help='switch between html and plain text message',
    forced={'mimepart': 'toggle'},
    arguments=[(['query'], {'help': 'query used to filter messages to affect',
                            'nargs': '*'})])
class ChangeDisplaymodeCommand(Command):

    """fold or unfold messages"""
    repeatable = True

    def __init__(self, query=None, visible=None, raw=None, all_headers=None,
                 indent=None, mimetree=None, mimepart=False, **kwargs):
        """
        :param query: notmuch query string used to filter messages to affect
        :type query: str
        :param visible: unfold if `True`, fold if `False`, ignore if `None`
        :type visible: True, False, 'toggle' or None
        :param raw: display raw message text
        :type raw: True, False, 'toggle' or None
        :param all_headers: show all headers (only visible if not in raw mode)
        :type all_headers: True, False, 'toggle' or None
        :param indent: message/reply indentation
        :type indent: '+', '-', or int
        :param mimetree: show the mime tree of the message
        :type mimetree: True, False, 'toggle' or None
        """
        self.query = None
        if query:
            self.query = ' '.join(query)
        self.visible = visible
        self.raw = raw
        self.all_headers = all_headers
        self.indent = indent
        self.mimetree = mimetree
        self.mimepart = mimepart
        Command.__init__(self, **kwargs)

    def _matches(self, msgt):
        if self.query is None or self.query == '*':
            return True
        msg = msgt.get_message()
        return msg.matches(self.query)

    def apply(self, ui):
        tbuffer = ui.current_buffer

        # set message/reply indentation if changed
        if self.indent is not None:
            if self.indent == '+':
                newindent = tbuffer._indent_width + 1
            elif self.indent == '-':
                newindent = tbuffer._indent_width - 1
            else:
                # argparse validation guarantees that self.indent
                # can be cast to an integer
                newindent = int(self.indent)
            # make sure indent remains non-negative
            tbuffer._indent_width = max(newindent, 0)
            tbuffer.rebuild()
            tbuffer.collapse_all()
            ui.update()

        logging.debug('matching lines %s...', self.query)
        if self.query is None:
            messagetrees = [tbuffer.get_selected_messagetree()]
        else:
            messagetrees = tbuffer.messagetrees()

        for mt in messagetrees:
            # determine new display values for this message
            if self.visible == 'toggle':
                visible = mt.is_collapsed(mt.root)
            else:
                visible = self.visible
            if not self._matches(mt):
                visible = not visible

            if self.raw == 'toggle':
                tbuffer.focus_selected_message()
            raw = not mt.display_source if self.raw == 'toggle' else self.raw
            all_headers = not mt.display_all_headers \
                if self.all_headers == 'toggle' else self.all_headers
            if self.mimepart:
                if self.mimepart == 'toggle':
                    message = mt.get_message()
                    mimepart = message.get_mime_part()
                    if mimepart is not None:
                        mimetype = {'plain': 'html', 'html': 'plain'}[
                            message.get_mime_part().get_content_subtype()]
                        mimepart = get_body_part(message.get_email(), mimetype)
                elif self.mimepart is True:
                    mimepart = ui.get_deep_focus().mimepart
                mt.set_mimepart(mimepart)
                ui.update()
            if self.mimetree == 'toggle':
                tbuffer.focus_selected_message()
            mimetree = not mt.display_mimetree \
                if self.mimetree == 'toggle' else self.mimetree

            # collapse/expand depending on new 'visible' value
            if visible is False:
                mt.collapse(mt.root)
            elif visible is True:  # could be None
                mt.expand(mt.root)
            tbuffer.focus_selected_message()
            # set new values in messagetree obj
            if raw is not None:
                mt.display_source = raw
            if all_headers is not None:
                mt.display_all_headers = all_headers
            if mimetree is not None:
                mt.display_mimetree = mimetree
            mt.debug()
            # let the messagetree reassemble itself
            mt.reassemble()
        # refresh the buffer (clears Tree caches etc)
        tbuffer.refresh()


@registerCommand(MODE, 'pipeto', arguments=[
    (['cmd'], {'help': 'shellcommand to pipe to', 'nargs': '+'}),
    (['--all'], {'action': 'store_true', 'help': 'pass all messages'}),
    (['--format'], {'help': 'output format', 'default': 'raw',
                    'choices': ['raw', 'decoded', 'id', 'filepath']}),
    (['--separately'], {'action': 'store_true',
                        'help': 'call command once for each message'}),
    (['--background'], {'action': 'store_true',
                        'help': 'don\'t stop the interface'}),
    (['--add_tags'], {'action': 'store_true',
                      'help': 'add \'Tags\' header to the message'}),
    (['--shell'], {'action': 'store_true',
                   'help': 'let the shell interpret the command'}),
    (['--notify_stdout'], {'action': 'store_true',
                           'help': 'display cmd\'s stdout as notification'}),
    (['--strip_ansi'], {'action': 'store_true',
                        'help': 'remove ANSI CSI escapes from the content'}),
])
class PipeCommand(Command):

    """pipe message(s) to stdin of a shellcommand"""
    repeatable = True

    def __init__(self, cmd, all=False, separately=False, background=False,
                 shell=False, notify_stdout=False, format='raw',
                 add_tags=False, strip_ansi=False, noop_msg='no command specified',
                 confirm_msg='', done_msg=None, **kwargs):
        """
        :param cmd: shellcommand to open
        :type cmd: str or list of str
        :param all: pipe all, not only selected message
        :type all: bool
        :param separately: call command once per message
        :type separately: bool
        :param background: do not suspend the interface
        :type background: bool
        :param shell: let the shell interpret the command
        :type shell: bool
        :param notify_stdout: display command\'s stdout as notification message
        :type notify_stdout: bool
        :param format: what to pipe to the processes stdin. one of:
            'raw': message content as is,
            'decoded': message content, decoded quoted printable,
            'id': message ids, separated by newlines,
            'filepath': paths to message files on disk
        :type format: str
        :param add_tags: add 'Tags' header to the message
        :type add_tags: bool
        :param strip_ansi: remove ANSI CSI escapes from the content
        :type strip_ansi: bool
        :param noop_msg: error notification to show if `cmd` is empty
        :type noop_msg: str
        :param confirm_msg: confirmation question to ask (continues directly if
                            unset)
        :type confirm_msg: str
        :param done_msg: notification message to show upon success
        :type done_msg: str
        """
        Command.__init__(self, **kwargs)
        if isinstance(cmd, str):
            cmd = split_commandstring(cmd)
        self.cmd = cmd
        self.whole_thread = all
        self.separately = separately
        self.background = background
        self.shell = shell
        self.notify_stdout = notify_stdout
        self.output_format = format
        self.add_tags = add_tags
        self.strip_ansi = strip_ansi
        self.noop_msg = noop_msg
        self.confirm_msg = confirm_msg
        self.done_msg = done_msg

    async def apply(self, ui):
        # abort if command unset
        if not self.cmd:
            ui.notify(self.noop_msg, priority='error')
            return

        # get messages to pipe
        if self.whole_thread:
            thread = ui.current_buffer.get_selected_thread()
            if not thread:
                return
            to_print = thread.get_messages().keys()
        else:
            to_print = [ui.current_buffer.get_selected_message()]

        # ask for confirmation if needed
        if self.confirm_msg:
            if (await ui.choice(self.confirm_msg, select='yes',
                                cancel='no')) == 'no':
                return

        # prepare message sources
        pipestrings = []
        separator = '\n\n'
        logging.debug('PIPETO format')
        logging.debug(self.output_format)

        if self.output_format == 'id':
            pipestrings = [e.get_message_id() for e in to_print]
            separator = '\n'
        elif self.output_format == 'filepath':
            pipestrings = [e.get_filename() for e in to_print]
            separator = '\n'
        else:
            for msg in to_print:
                mail = msg.get_email()
                if self.add_tags:
                    mail.add_header('Tags', ', '.join(msg.get_tags()))
                if self.output_format == 'raw':
                    pipestrings.append(mail.as_string())
                elif self.output_format == 'decoded':
                    headertext = extract_headers(mail)
                    bodytext = msg.get_body_text()
                    msgtext = '%s\n\n%s' % (headertext, bodytext)
                    pipestrings.append(msgtext)

        if self.strip_ansi:
            pipestrings = [ansi.strip_ansi_escapes(s) for s in pipestrings]

        if not self.separately:
            pipestrings = [separator.join(pipestrings)]
        if self.shell:
            self.cmd = [' '.join(self.cmd)]

        # do the monkey
        for mail in pipestrings:
            encoded_mail = mail.encode(urwid.util.detected_encoding)
            if self.background:
                logging.debug('call in background: %s', self.cmd)
                proc = subprocess.Popen(self.cmd,
                                        shell=True, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                out, err = proc.communicate(encoded_mail)
                if self.notify_stdout:
                    ui.notify(out)
            else:
                with ui.paused():
                    logging.debug('call: %s', self.cmd)
                    # if proc.stdout is defined later calls to communicate
                    # seem to be non-blocking!
                    proc = subprocess.Popen(self.cmd, shell=True,
                                            stdin=subprocess.PIPE,
                                            # stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    out, err = proc.communicate(encoded_mail)
            if err:
                ui.notify(err, priority='error')
                return

        # display 'done' message
        if self.done_msg:
            ui.notify(self.done_msg)


@registerCommand(MODE, 'remove', arguments=[
    (['--all'], {'action': 'store_true', 'help': 'remove whole thread'})])
class RemoveCommand(Command):

    """remove message(s) from the index"""
    repeatable = True

    def __init__(self, all=False, **kwargs):
        """
        :param all: remove all messages from thread, not just selected one
        :type all: bool
        """
        Command.__init__(self, **kwargs)
        self.all = all

    async def apply(self, ui):
        threadbuffer = ui.current_buffer
        # get messages and notification strings
        if self.all:
            thread = threadbuffer.get_selected_thread()
            tid = thread.get_thread_id()
            messages = thread.get_messages().keys()
            confirm_msg = 'remove all messages in thread?'
            ok_msg = 'removed all messages in thread: %s' % tid
        else:
            msg = threadbuffer.get_selected_message()
            messages = [msg]
            confirm_msg = 'remove selected message?'
            ok_msg = 'removed message: %s' % msg.get_message_id()

        # ask for confirmation
        if (await ui.choice(confirm_msg, select='yes', cancel='no')) == 'no':
            return

        # notify callback
        def callback():
            threadbuffer.rebuild()
            ui.notify(ok_msg)

        # remove messages
        for m in messages:
            ui.dbman.remove_message(m, afterwards=callback)

        await ui.apply_command(FlushCommand())


@registerCommand(MODE, 'print', arguments=[
    (['--all'], {'action': 'store_true', 'help': 'print all messages'}),
    (['--raw'], {'action': 'store_true', 'help': 'pass raw mail string'}),
    (['--separately'], {'action': 'store_true',
                        'help': 'call print command once for each message'}),
    (['--add_tags'], {'action': 'store_true',
                      'help': 'add \'Tags\' header to the message'}),
])
class PrintCommand(PipeCommand):

    """print message(s)"""
    repeatable = True

    def __init__(self, all=False, separately=False, raw=False, add_tags=False,
                 **kwargs):
        """
        :param all: print all, not only selected messages
        :type all: bool
        :param separately: call print command once per message
        :type separately: bool
        :param raw: pipe raw message string to print command
        :type raw: bool
        :param add_tags: add 'Tags' header to the message
        :type add_tags: bool
        """
        # get print command
        cmd = settings.get('print_cmd') or ''

        # set up notification strings
        if all:
            confirm_msg = 'print all messages in thread?'
            ok_msg = 'printed thread using %s' % cmd
        else:
            confirm_msg = 'print selected message?'
            ok_msg = 'printed message using %s' % cmd

        # no print cmd set
        noop_msg = 'no print command specified. Set "print_cmd" in the '\
            'global section.'

        PipeCommand.__init__(self, [cmd], all=all, separately=separately,
                             background=True,
                             shell=False,
                             format='raw' if raw else 'decoded',
                             add_tags=add_tags,
                             noop_msg=noop_msg, confirm_msg=confirm_msg,
                             done_msg=ok_msg, **kwargs)


@registerCommand(MODE, 'save', arguments=[
    (['--all'], {'action': 'store_true', 'help': 'save all attachments'}),
    (['path'], {'nargs': '?', 'help': 'path to save to'})])
class SaveAttachmentCommand(Command):

    """save attachment(s)"""
    def __init__(self, all=False, path=None, **kwargs):
        """
        :param all: save all, not only selected attachment
        :type all: bool
        :param path: path to write to. if `all` is set, this must be a
                     directory.
        :type path: str
        """
        Command.__init__(self, **kwargs)
        self.all = all
        self.path = path

    async def apply(self, ui):
        pcomplete = PathCompleter()
        savedir = settings.get('attachment_prefix', '~')
        if self.all:
            msg = ui.current_buffer.get_selected_message()
            if not self.path:
                self.path = await ui.prompt('save attachments to',
                                            text=os.path.join(savedir, ''),
                                            completer=pcomplete)
            if self.path:
                if os.path.isdir(os.path.expanduser(self.path)):
                    for a in msg.get_attachments():
                        dest = a.save(self.path)
                        name = a.get_filename()
                        if name:
                            ui.notify('saved %s as: %s' % (name, dest))
                        else:
                            ui.notify('saved attachment as: %s' % dest)
                else:
                    ui.notify('not a directory: %s' % self.path,
                              priority='error')
            else:
                raise CommandCanceled()
        else:  # save focussed attachment
            focus = ui.get_deep_focus()
            if isinstance(focus, AttachmentWidget):
                attachment = focus.get_attachment()
                filename = attachment.get_filename()
                if not self.path:
                    msg = 'save attachment (%s) to ' % filename
                    initialtext = os.path.join(savedir, filename)
                    self.path = await ui.prompt(msg,
                                                completer=pcomplete,
                                                text=initialtext)
                if self.path:
                    try:
                        dest = attachment.save(self.path)
                        ui.notify('saved attachment as: %s' % dest)
                    except (IOError, OSError) as e:
                        ui.notify(str(e), priority='error')
                else:
                    raise CommandCanceled()


class OpenAttachmentCommand(Command):

    """displays an attachment according to mailcap"""
    def __init__(self, attachment, **kwargs):
        """
        :param attachment: attachment to open
        :type attachment: :class:`~alot.db.attachment.Attachment`
        """
        Command.__init__(self, **kwargs)
        self.attachment = attachment

    async def apply(self, ui):
        logging.info('open attachment')
        mimetype = self.attachment.get_content_type()

        # returns pair of preliminary command string and entry dict containing
        # more info. We only use the dict and construct the command ourselves
        _, entry = settings.mailcap_find_match(mimetype)
        if entry:
            afterwards = None  # callback, will rm tempfile if used
            handler_stdin = None
            tempfile_name = None
            handler_raw_commandstring = entry['view']
            # read parameter
            part = self.attachment.get_mime_representation()
            parms = tuple('='.join(p) for p in part.get_params())

            # in case the mailcap defined command contains no '%s',
            # we pipe the files content to the handling command via stdin
            if '%s' in handler_raw_commandstring:
                nametemplate = entry.get('nametemplate', '%s')
                prefix, suffix = parse_mailcap_nametemplate(nametemplate)

                fn_hook = settings.get_hook('sanitize_attachment_filename')
                if fn_hook:
                    # get filename
                    filename = self.attachment.get_filename()
                    prefix, suffix = fn_hook(filename, prefix, suffix)

                with tempfile.NamedTemporaryFile(delete=False, prefix=prefix,
                                                 suffix=suffix) as tmpfile:
                    tempfile_name = tmpfile.name
                    self.attachment.write(tmpfile)

                def afterwards():
                    os.unlink(tempfile_name)
            else:
                handler_stdin = BytesIO()
                self.attachment.write(handler_stdin)

            # create handler command list
            handler_cmd = mailcap.subst(handler_raw_commandstring, mimetype,
                                        filename=tempfile_name, plist=parms)

            handler_cmdlist = split_commandstring(handler_cmd)

            # 'needsterminal' makes handler overtake the terminal
            # XXX: could this be repalced with "'needsterminal' not in entry"?
            overtakes = entry.get('needsterminal') is None

            await ui.apply_command(ExternalCommand(handler_cmdlist,
                                                   stdin=handler_stdin,
                                                   on_success=afterwards,
                                                   thread=overtakes))
        else:
            ui.notify('unknown mime type')


@registerCommand(
    MODE, 'move', help='move focus in current buffer',
    arguments=[
        (['movement'],
         {'nargs': argparse.REMAINDER,
          'help': '''up, down, [half]page up, [half]page down, first, last, \
                  parent, first reply, last reply, \
                  next sibling, previous sibling, next, previous, \
                  next unfolded, previous unfolded, \
                  next NOTMUCH_QUERY, previous NOTMUCH_QUERY'''})])
class MoveFocusCommand(MoveCommand):

    def apply(self, ui):
        logging.debug(self.movement)
        original_focus = ui.get_deep_focus()
        tbuffer = ui.current_buffer
        if self.movement == 'parent':
            tbuffer.focus_parent()
        elif self.movement == 'first reply':
            tbuffer.focus_first_reply()
        elif self.movement == 'last reply':
            tbuffer.focus_last_reply()
        elif self.movement == 'next sibling':
            tbuffer.focus_next_sibling()
        elif self.movement == 'previous sibling':
            tbuffer.focus_prev_sibling()
        elif self.movement == 'next':
            tbuffer.focus_next()
        elif self.movement == 'previous':
            tbuffer.focus_prev()
        elif self.movement == 'next unfolded':
            tbuffer.focus_next_unfolded()
        elif self.movement == 'previous unfolded':
            tbuffer.focus_prev_unfolded()
        elif self.movement.startswith('next '):
            query = self.movement[5:].strip()
            tbuffer.focus_next_matching(query)
        elif self.movement.startswith('previous '):
            query = self.movement[9:].strip()
            tbuffer.focus_prev_matching(query)
        else:
            MoveCommand.apply(self, ui)
        # TODO add 'next matching' if threadbuffer stores the original query
        # TODO: add next by date..

        if original_focus != ui.get_deep_focus():
            ui.update()


@registerCommand(MODE, 'select')
class ThreadSelectCommand(Command):

    """select focussed element:
        - if it is a message summary, toggle visibility of the message;
        - if it is an attachment line, open the attachment
        - if it is a mimepart, toggle visibility of the mimepart"""
    async def apply(self, ui):
        focus = ui.get_deep_focus()
        if isinstance(focus, AttachmentWidget):
            logging.info('open attachment')
            await ui.apply_command(OpenAttachmentCommand(focus.get_attachment()))
        elif getattr(focus, 'mimepart', False):
            if isinstance(focus.mimepart, Attachment):
                await ui.apply_command(OpenAttachmentCommand(focus.mimepart))
            else:
                await ui.apply_command(ChangeDisplaymodeCommand(
                    mimepart=True, mimetree='toggle'))
        else:
            await ui.apply_command(ChangeDisplaymodeCommand(visible='toggle'))


RetagPromptCommand = registerCommand(MODE, 'retagprompt')(RetagPromptCommand)


@registerCommand(
    MODE, 'tag', forced={'action': 'add'},
    arguments=[
        (['--all'], {'action': 'store_true',
                     'help': 'tag all messages in thread'}),
        (['--no-flush'], {'action': 'store_false', 'dest': 'flush',
                          'help': 'postpone a writeout to the index'}),
        (['tags'], {'help': 'comma separated list of tags'})],
    help='add tags to message(s)',
)
@registerCommand(
    MODE, 'retag', forced={'action': 'set'},
    arguments=[
        (['--all'], {'action': 'store_true',
                     'help': 'tag all messages in thread'}),
        (['--no-flush'], {'action': 'store_false', 'dest': 'flush',
                          'help': 'postpone a writeout to the index'}),
        (['tags'], {'help': 'comma separated list of tags'})],
    help='set message(s) tags.',
)
@registerCommand(
    MODE, 'untag', forced={'action': 'remove'},
    arguments=[
        (['--all'], {'action': 'store_true',
                     'help': 'tag all messages in thread'}),
        (['--no-flush'], {'action': 'store_false', 'dest': 'flush',
                          'help': 'postpone a writeout to the index'}),
        (['tags'], {'help': 'comma separated list of tags'})],
    help='remove tags from message(s)',
)
@registerCommand(
    MODE, 'toggletags', forced={'action': 'toggle'},
    arguments=[
        (['--all'], {'action': 'store_true',
                     'help': 'tag all messages in thread'}),
        (['--no-flush'], {'action': 'store_false', 'dest': 'flush',
                          'help': 'postpone a writeout to the index'}),
        (['tags'], {'help': 'comma separated list of tags'})],
    help='flip presence of tags on message(s)',
)
class TagCommand(Command):

    """manipulate message tags"""
    repeatable = True

    def __init__(self, tags='', action='add', all=False, flush=True,
                 **kwargs):
        """
        :param tags: comma separated list of tagstrings to set
        :type tags: str
        :param action: adds tags if 'add', removes them if 'remove', adds tags
                       and removes all other if 'set' or toggle individually if
                       'toggle'
        :type action: str
        :param all: tag all messages in thread
        :type all: bool
        :param flush: immediately write out to the index
        :type flush: bool
        """
        self.tagsstring = tags
        self.all = all
        self.action = action
        self.flush = flush
        Command.__init__(self, **kwargs)

    async def apply(self, ui):
        tbuffer = ui.current_buffer
        if self.all:
            messagetrees = tbuffer.messagetrees()
            testquery = "thread:%s" % \
                        tbuffer.get_selected_thread().get_thread_id()
        else:
            messagetrees = [tbuffer.get_selected_messagetree()]
            testquery = "mid:%s" % tbuffer.get_selected_mid()

        def refresh():
            for mt in messagetrees:
                mt.refresh()

            # put currently selected message id on a block list for the
            # auto-remove-unread feature. This makes sure that explicit
            # tag-unread commands for the current message are not undone on the
            # next keypress (triggering the autorm again)...
            mid = tbuffer.get_selected_mid()
            tbuffer._auto_unread_dont_touch_mids.add(mid)

            tbuffer.refresh()

        tags = [t for t in self.tagsstring.split(',') if t]

        try:
            if self.action == 'add':
                ui.dbman.tag(testquery, tags, remove_rest=False,
                             afterwards=refresh)
            if self.action == 'set':
                ui.dbman.tag(testquery, tags, remove_rest=True,
                             afterwards=refresh)
            elif self.action == 'remove':
                ui.dbman.untag(testquery, tags, afterwards=refresh)
            elif self.action == 'toggle':
                ui.dbman.toggle_tags(testquery, tags, afterwards=refresh)

        except DatabaseROError:
            ui.notify('index in read-only mode', priority='error')
            return

        # flush index
        if self.flush:
            await ui.apply_command(FlushCommand())
