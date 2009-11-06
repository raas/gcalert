#!/usr/bin/python
# vim: ai expandtab

# This script will periodically check your Google Calendar and display an alarm
# when that's set for the event. It reads ALL your calendars automatically.
#
# Requires: python-notify python-gdata
# Also, recommended for SSL: /afs/cern.ch/user/a/ahorvath/public/deb/python-gdata_1.2.4-0ubuntu2ssl_all.deb FIXME
# Edit below for username/pass
#
# FIXME:
# - time zones (different in calendar than on localhost) !!! Each calendar can have different TZ set! Fcuk
# - gracious handling of missing pynotify 
# - command-line options
# - config file for username/password
# - funky icon for libnotify alert:)
# - add 'Location' string from feed

try:
    from xml.etree import ElementTree # for Python 2.5 users
except ImportError:
    from elementtree import ElementTree

from gdata.calendar.service import *
import gdata.service
import atom.service
import gdata.calendar
import atom
import getopt
import sys
import string
import time
import urllib
import pynotify

# ----------------------------
sleeptime=30 # seconds
gcal_time_format='%Y-%m-%dT%H:%M:%S.000+01:00'  # FIXME timezone vs local timezone??!!
# ----------------------------

events=[] # all events seen so far that are yet to start
alarmed_events=[] # events (occurences etc) already done, minus those in the past

# ----------------------------
# get the list of 'magic strings' used to identify each calendar
# returns: list(username) that each can be used in CalendarEventQuery()
#
def GetUserCalendars(cs):
    feed = cs.GetAllCalendarsFeed()
    # in there is the full feed URL and we need the last part (=='username')
    return map(lambda x: urllib.unquote(x.id.text.split('/')[-1]), feed.entry) 

# ----------------------------
# get a list of events happening between the given dates
# in all calendars the user has
# return: list of events
#
# each event record has fields 'title', 'start', 'end', 'minutes' 
# each reminder occurence creates a new event
#
def DateRangeQuery(cs, start_date='2007-01-01', end_date='2007-07-01'):
    el=[] # event occurence list
    for username in GetUserCalendars(cs):
        query = gdata.calendar.service.CalendarEventQuery(username, 'private', 'full')
        query.start_min = start_date
        query.start_max = end_date 
        feed = cs.CalendarQuery(query)
        for an_event in feed.entry:
            for a_when in an_event.when:
                for a_rem in a_when.reminder:
                    # it's a separate 'event' for each reminder
                    el.append({'title':an_event.title.text, 
                               'start':a_when.start_time,
                               'end':a_when.end_time,
                               'minutes':a_rem.minutes})
    return el

# ----------------------------

# alarm one event
def do_alarm(event):
    # FIXME: time format
    starttime=time.strftime('%Y-%m-%d %H:%M:%S',time.strptime(event['start'],gcal_time_format))
    print " ***** ALARM ALARM ALARM %s %s ****  " % ( event['title'],starttime ) 
    # FIXME add an icon here
    a=pynotify.Notification( event['title'], "Starting: %s" % starttime )
    # let the alarm stay until it's closed by hand (acknowledged)
    a.set_timeout(0)
    if not a.show():
        print "Failed to send notification!"


# ----------------------------

# initialize alarm system
if not pynotify.init("Basics"):
    sys.exit(1)

# login
cs = CalendarService()
cs.email = 'probabela98'
# this needs the SSL patch to python-gdata
# if not present, the login will go over SSL
# but the actual calendar will be retrieved over plain HTTP
# tcpdump if unsure ;)
cs.ssl = True;
cs.password = 'pbela123'
cs.source = 'raas-Calendar_Alerter-0.1'
try:
    cs.ProgrammaticLogin()
except gdata.service.Error: # seriously, yes
    print 'Failed to authenticate to google.'
    print 'Check username, password and that the account is enabled.'
    sys.exit(1)

# ----------------------------
while 1:
    print "---- iteration ----"
    newevents=DateRangeQuery(cs, '2009-11-06','2009-11-07')
    nowstring=time.strftime(gcal_time_format,time.localtime())
    nowunixtime=time.time()
    # add new events to the list
    for n in newevents:
        if not n in events:
            print 'Received event: %s' % n
            # does it start in the future?
            if nowstring<n['start']:
                print "-> future, adding"
                events.append(n)
            else:
                print "-> past already"
    # throw away old events
    for e in events:
        if e['start']<nowstring:
            print "removing %s, is gone" % e
            events.remove(e)
            # also free up some memory
            if e in alarmed_events:
                alarmed_events.remove(e)
        # it starts in the future
        # check for alarm times if it wasn't alarmed yet
        elif e not in alarmed_events:
            # calculate alarm time. If it's now-ish, raise alarm
            # otherwise, let the event sleep some more
            alarm_at_unixtime=int(time.strftime('%s',(time.strptime(e['start'],gcal_time_format))))-60*int(e['minutes'])
            # alarm now if the alarm has 'started'
            if nowunixtime >= alarm_at_unixtime:
                do_alarm(e)
                alarmed_events.append(e)
            else:
                print "no alarm yet for %s" % e
    print "...now sleeping %d seconds" % sleeptime
    time.sleep(sleeptime)
