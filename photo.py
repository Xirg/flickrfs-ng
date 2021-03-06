import anydbm
import cPickle
import contextlib
import logging
import thread, threading
import urllib
import os
from stat import S_IFREG
from traceback import format_exc
from collections import defaultdict

from libs.python_flickr_api import flickr_api as flickr
from libs.requests import requests

log = logging.getLogger('flickrfs-ng')

#Utility functions.
NUMRETRIES = 3
def _log_exception_wrapper(func, *args, **kw):
  """Call 'func' with args and kws and log any exception it throws.
  """
  for i in range(0, NUMRETRIES):
    log.debug("retry attempt %s for func %s", i, func.__name__)
    try:
      func(*args, **kw)
      return
    except:
      log.error("exception in function %s", func.__name__)
      log.error(format_exc())

def getTakenDateStr(photo):
  return photo.taken.split(' ', 1)[0]

def getTakenDate(photo):
  import time
  return time.mktime(time.strptime("%Y-%m-%d %H:%M:%S", photo.taken))

def _get_unix_perms(isfriend, isfamily, ispublic):
  perms = 0744
  if isfriend:
    perms |= 0010
  if isfamily:
    perms |= 0020
  if ispublic:
    perms |= 0011
  return perms

def photo_url_reducer(url_dict, photo):
  from urlparse import urlparse
  o = urlparse(photo.url)
  url_dict[o.hostname].append(photo)
  return url_dict

def fetch_photo_size(photos):
  s = requests.Session()
  for photo in photos:
    r = s.head(photo.url)
    size = int(r.headers.get('content-length'))
    photo.inode['st_size'] = photo.size = size
    log.debug("url:%s size:%d" % (photo.url, size))

def _batch_fetch_size(photo_list):
  photo_group = reduce(photo_url_reducer, photo_list, defaultdict(list))
  [thread.start_new_thread(fetch_photo_size, (photo_group[k], )) for k in photo_group.keys()]

class PhotoStream:
  def __init__(self, inode, path, user):
    self.stream_inode = inode
    self.path = path
    self.photos = dict()
    self.syncer = PhotoSyncer(user, self.add_photo)
    self.syncer.start_sync_thread()

  def add_photo(self, photo):
    photo.filename = self._get_filename(photo)
    photo.inode = self.stream_inode.mknod(st_mode = S_IFREG | photo.mode,
                                          st_ctime = photo.upload,
                                          st_mtime = photo.update)
    self.photos[photo.filename] = photo

  def getattr(self, path, fh=None):
    (parent, base) = path.rsplit('/', 1)
    assert parent == self.path
    if base not in self.photos:
      return None
    return self.photos[base].inode.getattrs()

  def read(self, base, size, offset):
    return self.photos[base].get_data(offset, offset + size)

  def prefetch_file(self, base):
    photo = self.photos.get(base, None)
    assert photo
    photo.prefetch_file_data()

  def file_list(self):
    return self.photos.keys()

  def _get_filename(self, photo):
    existing = self.photos.keys()
    filebase = photo.title
    if len(filebase) == 0:
      filebase = getTakenDateStr(photo)
    while (filebase + "." + photo.ext) in existing:
      filesplit = filebase.rsplit(' ', 1)
      num = 0
      if len(filesplit) == 2:
        try:
          num = int(filesplit[1])
        except ValueError:
          pass
      if num > 0:
        num += 1
        new_base = filesplit[0]
      else:
        num = 1
        new_base = filebase
      filebase = "%s %03d" % (new_base, num)
    return "%s.%s" % (filebase, photo.ext)


class PhotoSyncer:
  def __init__(self, user, add_photo, sync_interval=300):
    self.user = user
    self.add_photo_func = add_photo
    self.sync_interval = sync_interval

  def _populate_stream_thread(self):
    log.info("populate_stream_thread start")
    pages = 1
    current_page = 1
    all_photos = []
    while current_page <= pages:
      photos = self.user.getPhotos(per_page=500, page=current_page,
                                   extras="original_format,last_update,date_upload,date_taken,url_o")
      pages = photos.info.pages
      for p in photos:
        photo = Photo(p)
        self.add_photo_func(photo)
        all_photos.append(photo)
      current_page += 1
    log.info("populate_stream_thread update sizes")
    _batch_fetch_size(all_photos)
    log.info("populate_stream_thread end")

  def _run_in_background(self, func, *args, **kw):
    thread.start_new_thread(_log_exception_wrapper, (func,) + args, )

  def start_sync_thread(self):
    self._run_in_background(self._populate_stream_thread)

class Photo(object):
  def __init__(self, photo):
    self.id = photo.id
    self.title = photo.title.replace('/', '_')
    self.mode = _get_unix_perms(photo.isfriend, photo.isfamily, photo.ispublic)
    self.ext = photo.originalformat
    self.taken = photo.datetaken
    self.upload = int(photo.dateupload)
    self.update = photo.lastupdate
    self.url = photo.url_o
    self.filename = None
    self.inode = None
    self.size = 0
    self.data_fetching_thread = None

  def fetch_size(self):
    if self.size == 0:
      assert len(self.url) > 0
      log.info('requesting ' + self.url)
      with contextlib.closing(urllib.urlopen(self.url)) as d:
        self.size = int(d.info()['Content-Length'])
      log.info('size: %d' % self.size)
    self.inode['st_size'] = self.size

  def read_file(self):
    cache = PhotoCache()
    log.info("read_file")
    data = (yield)
    cache[self.id] = data
    log.info("read_file done")
    yield len(data)

  def fetch_file(self, target):
    r = requests.get(self.url)
    assert r.status_code == 200
    yield target.send(r.content)

  def prefetch_file_data(self):
    self.fetch_size()

  def get_data(self, start=0, end=0):
    cache = PhotoCache()
    if self.id not in cache.keys():
      read_file = self.read_file()
      read_file.next()
      fetch_file = self.fetch_file(read_file)
      size = fetch_file.next()
      log.info("fetch_file done, %d" % size)
      assert self.id in cache.keys()
      fetch_file.close()
      read_file.close()
    data = cache[self.id]
    assert data
    if end == 0:
      return data[start:]
    else:
      return data[start:end]


class PhotoCache(object):
  cache_file = None
  _instance = None

  def __new__(class_, *args, **kwargs):
    if not isinstance(class_._instance, class_):
        class_._instance = object.__new__(class_, *args, **kwargs)
        class_._instance.__init_once__()
    return class_._instance

  def __init_once__(self, max_items=10):
    assert PhotoCache.cache_file is not None
    self.db = anydbm.open(PhotoCache.cache_file, flag='n')
    self._key_order = self.db.keys()
    self._max_items = max_items

  def __getitem__(self, key, default=None):
    if key not in self._key_order:
      return default
    self._mark(key)
    v = self.db.get(str(key))
    return cPickle.loads(v)

  def __setitem__(self, key, value):
    self.db[str(key)] = cPickle.dumps(value)
    self._mark(key)

  def _mark(self, key):
    if key in self._key_order:
      self._key_order.remove(key)

    self._key_order.insert(0, key)
    if len(self._key_order) > self._max_items:
      remove_key = self._key_order[self._max_items]
      del self.db[str(remove_key)]
      self._key_order.remove(remove_key)

  def keys(self):
    return sorted(self._key_order)

  @staticmethod
  def set_cache_file(file):
    PhotoCache.cache_file = file
